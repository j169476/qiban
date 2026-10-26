"""Read-only database producer feeding P1's encrypted, acknowledged inbox.

The source position commits with the entire batch before core delivery. The
consumer cursor only acknowledges that queue after the core event transaction.
Neither reconnect nor restart silently replaces an existing source baseline.
"""
from __future__ import annotations

import asyncio
import time

from companion_core.contracts import stable_key
from companion_core.errors import AssistantError

from .bindings import BindingRegistry, ConversationBinding
from .connector import WeChatConnector
from .contracts import Connection, Observation, digest, identifier, require
from .db_source import SourceError, validate_checkpoint, validate_enrollment
from .durable_inbox import DurableInbox
from .receiver_transport import SubprocessTransport, WorkerError

_ROW_FIELDS = {"target_ref", "kind", "server_id", "sender_ref", "text", "occurred_at",
               "direction", "trigger", "time_basis"}


def _snapshot(value):
    require(isinstance(value, dict) and set(value) == {"enrollment", "checkpoint"}, "wechat_source_invalid")
    try:
        enrollment = validate_enrollment(value["enrollment"])
        checkpoint = validate_checkpoint(value["checkpoint"], enrollment)
        require(all(identifier(t["username"]) for t in enrollment["targets"].values()), "wechat_source_invalid")
        require(identifier(enrollment["self_username"]), "wechat_source_invalid")
        return {"enrollment": enrollment, "checkpoint": checkpoint}
    except SourceError:
        raise AssistantError("wechat_source_invalid", "只读数据库源返回了无效身份或游标") from None


def _advance(before, after, *, opening=False):
    require(before["enrollment"] == after["enrollment"], "wechat_account_mismatch")
    old, new = before["checkpoint"]["sources"], after["checkpoint"]["sources"]
    require(set(old) <= set(new), "wechat_source_regressed")
    for key, previous in old.items():
        current = new[key]
        require(all(current[k] == previous[k] for k in ("shard", "target_ref", "salt")),
                "wechat_source_generation_changed")
        anchor = previous["anchor"]
        if opening:
            require(current == previous, "wechat_source_regressed")
        elif anchor is not None:
            newer = current["anchor"]
            require(newer is not None and newer["local_id"] >= anchor["local_id"], "wechat_source_regressed")
            require(newer["local_id"] != anchor["local_id"] or newer == anchor, "wechat_source_regressed")


class DatabaseGateway:
    simulation = False

    def __init__(self, store, config, *, account="wechat-local", transport_factory=None,
                 clock=time.monotonic, capacity=200, retry_initial=1.0, retry_max=60.0):
        require(identifier(account) and 0 < retry_initial <= retry_max <= 300)
        self.store, self.config, self.account = store, config, account
        self.transport_factory = transport_factory or SubprocessTransport
        self.clock, self.capacity = clock, capacity
        self.retry_initial, self.retry_max = retry_initial, retry_max
        self.pointer_key = "wechat-db-enrollment:" + stable_key("logical-account", account)
        self.source_key = "database-source:" + stable_key("logical-account", account)
        self.connector = self.inbox = self.bindings = None
        self._saved = self._worker = None
        self._closed, self._fatal = False, False
        self._next_retry, self._failures = 0.0, 0
        self._state, self._code = "new", ""
        self._reconnects = self._queued = self._skipped = self._blocked = 0
        self._lock = asyncio.Lock()

    def _initialize(self, pointer):
        pointer = _snapshot(pointer)
        enrollment = pointer["enrollment"]
        identity = enrollment["account_id"]
        # One verified WeChat identity has one logical collector consumer. A
        # different account cannot steal its durable stream or acknowledgments.
        owner_key = "wechat-db-owner:" + stable_key("identity", identity)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self.store.state(owner_key, None, db)
            require(owner is None or owner == self.account, "wechat_receiver_owner_conflict")
            if owner is None:
                self.store.set_state(owner_key, self.account, db)
        self.inbox = DurableInbox(self.store, identity, capacity=self.capacity)
        self._saved = self.inbox.checkpoint(self.source_key)
        if self._saved is None:
            self.inbox.publish_batch([], checkpoint_key=self.source_key,
                                     expected_checkpoint=None, checkpoint=pointer)
            self._saved = pointer
        self._saved = _snapshot(self._saved)
        require(self._saved["enrollment"] == enrollment, "wechat_account_mismatch")
        self.bindings = BindingRegistry(self.store, self.account, identity)
        targets = enrollment["targets"]
        owner = targets.get("owner_main", {}).get("username")
        peers = {owner: owner} if owner else {}
        expected = [ConversationBinding(alias, t["username"], t["kind"],
                    t["username"] if t["kind"] == "private" else "",
                    members=peers if t["kind"] == "group" else {}) for alias, t in targets.items()]
        for binding in expected:
            try:
                current = self.bindings.get(binding.key)
            except AssistantError as exc:
                if exc.code != "wechat_target_unbound":
                    raise
                self.bindings.bind(binding)
            else:
                require(current.enabled and all(getattr(current, k) == getattr(binding, k)
                        for k in ("target_ref", "kind", "peer_ref", "members")), "wechat_binding_changed")
        self.connector = WeChatConnector(self.bindings, self, None, read_only_collection=True, verified_sender_ids=True)

    async def start(self):
        async with self._lock:
            require(not self._closed and self.connector is None, "wechat_receiver_state")
            pointer = self.store.state(self.pointer_key)
            if pointer is not None:
                self._initialize(pointer)
            try:
                await self._open()
            except WorkerError as exc:
                await self._failed(exc)
                if self.connector is None:
                    raise
            except BaseException:
                await self._disconnect()
                raise
        return self.status()

    async def _open(self):
        self._worker = self.transport_factory()
        previous = self._saved
        result = await self._worker.request("open", config=self.config,
            enrollment=previous["enrollment"] if previous else None,
            checkpoint=previous["checkpoint"] if previous else None)
        require(not self._closed, "wechat_receiver_closed")
        snapshot = _snapshot(result)
        if previous is None:
            # Persist the very first baseline before any message is read. A
            # crash while installing bindings/queue can resume this same point.
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                pointer = self.store.state(self.pointer_key, None, db)
                require(pointer is None or pointer == snapshot, "wechat_checkpoint_conflict")
                self.store.set_state(self.pointer_key, snapshot, db)
            self._initialize(snapshot)
        else:
            _advance(previous, snapshot, opening=True)
            self.inbox.publish_batch([], checkpoint_key=self.source_key,
                                     expected_checkpoint=previous, checkpoint=snapshot)
            self._saved = snapshot
        self._reconnects += 1
        self._state, self._code = "connected", ""
        self._next_retry = 0.0

    def _observations(self, rows):
        require(isinstance(rows, list) and len(rows) <= 100, "wechat_source_invalid")
        enrollment = self._saved["enrollment"]
        targets = {v["username"]: v["kind"] for v in enrollment["targets"].values()}
        result = []
        for row in rows:
            require(isinstance(row, dict) and set(row) == _ROW_FIELDS, "wechat_source_invalid")
            target = row["target_ref"]
            require(isinstance(target, str) and target in targets and row["kind"] == targets[target],
                    "wechat_target_unbound")
            native = row["server_id"]
            require(isinstance(native, str) and native.isascii() and native.isdecimal()
                    and 0 < len(native) <= 19 and 0 < int(native) <= 2**63 - 1,
                    "wechat_source_invalid")
            sender = row["sender_ref"]
            require(identifier(sender) and (targets[target] != "private"
                    or sender in {target, enrollment["self_username"]}), "wechat_sender_mismatch")
            expected_direction = "self" if sender == enrollment["self_username"] else "incoming"
            require(row["direction"] == expected_direction and row["time_basis"] == "server"
                    and row["trigger"] in ({"direct"} if targets[target] == "private" else {"none", "mention", "reply"}),
                    "wechat_source_invalid")
            # Server identity + conversation separates repeated text, shards,
            # and same-number IDs in different conversations. Never hash text.
            observation_id = digest([enrollment["account_id"], target, str(int(native))])[:32]
            result.append(Observation(observation_id, enrollment["account_id"], self.inbox.epoch,
                                      **{k: v for k, v in row.items() if k != "server_id"}))
        return result

    async def _disconnect(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            await worker.close()

    async def _failed(self, error):
        await self._disconnect()
        if self._closed:
            return
        self._failures += 1
        self._code = error.code
        self._fatal = not error.recoverable
        self._state = "blocked" if self._fatal else "retrying"
        delay = min(self.retry_max, self.retry_initial * 2 ** min(self._failures - 1, 16))
        self._next_retry = self.clock() + delay

    async def pump_once(self):
        async with self._lock:
            require(self.connector is not None and not self._closed, "wechat_receiver_state")
            if self._fatal or self.clock() < self._next_retry:
                return self.status()
            try:
                if self._worker is None:
                    await self._open()
                result = await self._worker.request("read", checkpoint=self._saved["checkpoint"],
                                                    limit=min(100, self.capacity))
                require(not self._closed, "wechat_receiver_closed")
                require(isinstance(result, dict) and set(result) == {"rows", "next_checkpoint", "blocked", "skipped"},
                        "wechat_source_invalid")
                require(isinstance(result["blocked"], list) and len(result["blocked"]) <= 32
                        and type(result["skipped"]) is int and 0 <= result["skipped"] <= 512,
                        "wechat_source_invalid")
                after = _snapshot({"enrollment": self._saved["enrollment"], "checkpoint": result["next_checkpoint"]})
                _advance(self._saved, after)
                observations = self._observations(result["rows"])
                count = self.inbox.publish_batch(observations, checkpoint_key=self.source_key,
                    expected_checkpoint=self._saved, checkpoint=after)
                self._saved = after
                self._queued += count
                self._skipped += result["skipped"]
                self._blocked = len(result["blocked"])
                self._failures, self._next_retry = 0, 0.0
                self._state = "waiting_for_source" if self._blocked else "connected"
                self._code = "source_rows_pending" if self._blocked else ""
            except WorkerError as exc:
                await self._failed(exc)
            except AssistantError as exc:
                if self._closed:
                    return self.status()
                if exc.code == "wechat_inbox_full":
                    # Consumer drains the encrypted queue; retry the exact same
                    # source checkpoint on the next iteration, no row loss.
                    self._state, self._code = "backpressure", exc.code
                else:
                    await self._disconnect()
                    self._fatal, self._state, self._code = True, "blocked", exc.code
                    raise
            except asyncio.CancelledError:
                await self._disconnect()
                raise
            except Exception:
                await self._disconnect()
                raise AssistantError("wechat_receiver_failed", "只读收件未提交，已保留原游标") from None
            return self.status()

    def status(self):
        return {"state": self._state, "code": self._code,
                "retry_in_seconds": round(max(0.0, self._next_retry - self.clock()), 3) if not self._fatal else 0,
                "reconnects": max(0, self._reconnects - 1), "queued": self._queued,
                "skipped": self._skipped, "blocked_rows": self._blocked,
                "worker_connected": self._worker is not None}

    def connection(self):
        # Already authenticated durable messages can drain while native WeChat
        # is disconnected. This epoch represents ingestion, not a live PID.
        return Connection(self.bindings.identity if self.bindings else "uninitialized",
                          self.inbox.epoch if self.inbox else "uninitialized",
                          self.connector is not None and not self._closed)

    async def acknowledge(self, cursor):
        require(self.inbox is not None and not self._closed, "wechat_disconnected")
        await self.inbox.acknowledge(cursor)

    async def poll(self, cursor, *, limit=100):
        require(self.inbox is not None and not self._closed, "wechat_disconnected")
        return await self.inbox.poll(cursor, limit=limit)

    async def prepare(self, request):
        raise AssistantError("wechat_read_only", "数据库连接器仅收件")

    async def commit(self, permit, *, guard):
        raise AssistantError("wechat_read_only", "数据库连接器仅收件")

    async def discard(self, ticket):
        pass

    def revoke_generation(self, generation):
        pass

    async def close(self):
        self._closed = True
        await self._disconnect()
        self._state = "closed"
