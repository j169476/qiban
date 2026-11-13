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
from companion_core.history_status import validate_history_status
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope

from .bindings import BindingRegistry, ConversationBinding
from .connector import WeChatConnector
from .contracts import Connection, Observation, digest, identifier, require
from .db_source import SourceError, enrollment_identity, validate_checkpoint, validate_enrollment
from .durable_inbox import DurableInbox
from .receive_window import complete as window_complete
from .receive_window import validate_window
from .receiver_transport import SubprocessTransport, WorkerError

_ROW_FIELDS = {"target_ref", "kind", "server_id", "sender_ref", "text", "occurred_at",
               "direction", "trigger", "time_basis"}


def _snapshot(value):
    require(isinstance(value, dict) and {"enrollment", "checkpoint"} <= set(value)
            and not set(value) - {"enrollment", "checkpoint", "window", "history"}, "wechat_source_invalid")
    try:
        enrollment = validate_enrollment(value["enrollment"])
        checkpoint = validate_checkpoint(value["checkpoint"], enrollment)
        require(all(identifier(t["username"]) for t in enrollment["targets"].values()), "wechat_source_invalid")
        require(identifier(enrollment["self_username"]), "wechat_source_invalid")
        result = {"enrollment": enrollment, "checkpoint": checkpoint}
        if "window" in value:
            result["window"] = validate_window(value["window"], result)
        if "history" in value:
            history = value["history"]
            require(isinstance(history, dict) and set(history) <= set(enrollment["targets"]), "wechat_source_invalid")
            result["history"] = {alias: validate_history_status(status) for alias, status in history.items()}
            require(all(status is not None for status in result["history"].values()), "wechat_source_invalid")
        return result
    except SourceError:
        raise AssistantError("wechat_source_invalid", "只读数据库源返回了无效身份或游标") from None


def _advance(before, after, *, opening=False):
    require(enrollment_identity(before["enrollment"]) == enrollment_identity(after["enrollment"]), "wechat_account_mismatch")
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
                 clock=time.monotonic, capacity=200, retry_initial=1.0, retry_max=60.0, fixed_windows=False,
                 monitor_members=False):
        require(identifier(account) and 0 < retry_initial <= retry_max <= 300)
        self.store, self.config, self.account = store, config, account
        self.transport_factory = transport_factory or SubprocessTransport
        self.clock, self.capacity = clock, capacity
        require(type(fixed_windows) is bool, "wechat_receiver_state")
        self.fixed_windows = fixed_windows
        self.monitor_members = monitor_members
        from .membership import FollowDirectory
        self.follow_directory = FollowDirectory(store)
        from .follow_inbox import FollowInbox
        self.follow = FollowInbox(self)
        self.binding_added = None
        self._next_members = 0
        self._absent = set()
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
        require(enrollment_identity(self._saved["enrollment"]) == enrollment_identity(enrollment), "wechat_account_mismatch")
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
                fields = ("target_ref","kind","peer_ref") if enrollment["schema_version"] == 2 else (
                    "target_ref","kind","peer_ref","members")
                require(current.enabled and all(getattr(current, k) == getattr(binding, k)
                        for k in fields), "wechat_binding_changed")
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
        self._next_members = 0
        self._worker = self.transport_factory()
        previous = self._saved
        result = await self._worker.request("open", config=self.config,
            enrollment=previous["enrollment"] if previous else None,
            checkpoint=previous["checkpoint"] if previous else None,
            **({"recover_missing_history": True} if self.fixed_windows else {}))
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
            if "window" in previous:
                window = previous["window"]
                sources = dict(window["sources"])
                for key, item in snapshot["checkpoint"]["sources"].items():
                    if key not in sources:
                        sources[key] = {**item, "upper": item["anchor"]}
                snapshot = _snapshot({**snapshot, "window": {**window, "sources": sources}})
            if "history" in previous:
                snapshot = _snapshot({**snapshot, "history": previous["history"]})
            self.inbox.publish_batch([], checkpoint_key=self.source_key,
                                     expected_checkpoint=previous, checkpoint=snapshot)
            self._saved = snapshot
        self.store.set_state("wechat-display-names:" + snapshot["enrollment"]["account_id"],
                             snapshot["enrollment"].get("display_names", {}))
        self._reconnects += 1
        self._state, self._code = "connected", ""
        self._next_retry = 0.0

    def _sync_member_positions(self):
        """Rebase only changed relationships; keep unrelated unfinished windows and receipts."""
        before = self._saved
        if not before:
            return False
        enrollment = before["enrollment"]
        since = dict(enrollment.get("target_since", {}))
        reset, absent = set(), set()
        permissions = ReplyAuthorizations(self.store)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for alias, target in enrollment["targets"].items():
                record = permissions.record(ReplyScope("wechat", enrollment["account_id"],
                                             target["kind"], target["username"]), db=db)
                if record.get("member_present") is False:
                    absent.add(target["username"])
                base = since.setdefault(alias, enrollment["since"])
                if record.get("messages_since", 0) > base:
                    since[alias] = record["messages_since"]
                    reset.add(alias)
            self._absent = absent
            if not reset:
                return False
            updated = {**before, "enrollment": {**enrollment, "schema_version": 2, "target_since": since}}
            checkpoint = dict(before["checkpoint"])
            checkpoint["sources"] = {k: v for k, v in checkpoint["sources"].items() if v["target_ref"] not in reset}
            if checkpoint.get("next_source") not in checkpoint["sources"]:
                checkpoint.pop("next_source", None)
            updated["checkpoint"] = checkpoint
            if "window" in before:
                updated["window"] = {**before["window"], "sources": {
                    k: v for k,v in before["window"]["sources"].items() if k in checkpoint["sources"]}}
            if "history" in before:
                updated["history"] = {k:v for k,v in before["history"].items() if k not in reset}
            updated = _snapshot(updated)
            self.inbox.publish_batch([], checkpoint_key=self.source_key, expected_checkpoint=before, checkpoint=updated, db=db)
            self.store.set_state(self.pointer_key, {k:updated[k] for k in ("enrollment","checkpoint")}, db)
        self._saved = updated
        return True

    def ensure_follow_binding(self, target, kind, alias, db):
        known = self.store.state(self.bindings.key, db=db)["conversations"]
        if any(value["target_ref"] == target for value in known.values()):
            return
        binding = self.bindings.bind(ConversationBinding(alias,target,kind,target if kind=="private" else ""),db=db)
        if self.binding_added:
            self.binding_added(binding,db)

    def _observations(self, rows, enrollment=None):
        require(isinstance(rows, list) and len(rows) <= 100, "wechat_source_invalid")
        enrollment = enrollment or self._saved["enrollment"]
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

    async def resolve_alias(self, hint):
        async with self._lock:
            if self._closed or self._state != "connected" or self._worker is None:
                return None
            try:
                result = await self._worker.request("resolve_alias", hint=hint)
            except WorkerError as exc:
                await self._failed(exc)
                return None
            username = result.get("username")
            if username is None:
                return None
            binding = self.bindings.for_target(username)
            if binding.enabled and binding.kind == "private" and binding.peer_ref == username:
                return username
            return None

    async def _disconnect(self):
        self.follow_directory.disconnect()
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
                    if self.monitor_members:
                        self._sync_member_positions()
                    await self._open()
                if self.monitor_members and self.clock() >= self._next_members:
                    from .membership import MembershipCheck
                    enrolled = self._saved["enrollment"]
                    worker = self._worker
                    await self.follow_directory.refresh(enrolled["account_id"], worker,
                                                        lambda: not self._closed and self._worker is worker, find_chats=True)
                    check = MembershipCheck(self.store, enrolled["account_id"], enrolled["targets"].values())
                    check.finish(await self._worker.request("enrolled_members"))
                    if self._sync_member_positions():
                        await self._disconnect()
                        await self._open()
                        worker = self._worker
                        await self.follow_directory.refresh(enrolled["account_id"],worker,
                            lambda: not self._closed and self._worker is worker,find_chats=True)
                    self._next_members = self.clock() + 5
                if self.fixed_windows:
                    if self.follow_directory.chats:
                        queue = self.store.state(self.inbox.key)
                        if queue["acknowledged"] == queue["sequence"]:
                            await self.follow.pump()
                    if window_complete(self._saved):
                        queue = self.store.state(self.inbox.key)
                        if queue["acknowledged"] != queue["sequence"]:
                            return self.status()  # Drain before opening the next source range.
                    if "window" not in self._saved or window_complete(self._saved):
                        window = await self._worker.request("freeze_window", checkpoint=self._saved["checkpoint"])
                        frozen = _snapshot({**self._saved, "window": window, "history": {}})
                        require(all(item["anchor"] == self._saved["checkpoint"]["sources"][key]["anchor"]
                                    for key, item in frozen["window"]["sources"].items()), "wechat_source_regressed")
                        # Persist upper bounds before reading the first page.
                        self.inbox.publish_batch([], checkpoint_key=self.source_key,
                            expected_checkpoint=self._saved, checkpoint=frozen)
                        self._saved = frozen
                    result = await self._worker.request("read_window", checkpoint=self._saved["checkpoint"],
                        window=self._saved["window"], limit=min(100, self.capacity))
                else:
                    result = await self._worker.request("read", checkpoint=self._saved["checkpoint"],
                                                        limit=min(100, self.capacity))
                require(not self._closed, "wechat_receiver_closed")
                fields = {"rows", "next_checkpoint", "blocked", "skipped"}
                if self.fixed_windows:
                    fields |= {"window_complete"}
                require(isinstance(result, dict) and fields <= set(result)
                        and not set(result) - fields - ({"history_status"} if self.fixed_windows else set()),
                        "wechat_source_invalid")
                require(isinstance(result["blocked"], list) and len(result["blocked"]) <= 32
                        and type(result["skipped"]) is int and 0 <= result["skipped"] <= 512,
                        "wechat_source_invalid")
                after = _snapshot({"enrollment": self._saved["enrollment"], "checkpoint": result["next_checkpoint"]})
                if self.fixed_windows:
                    after = _snapshot({**after, "window": self._saved["window"]})
                    require(type(result["window_complete"]) is bool
                            and result["window_complete"] == window_complete(after), "wechat_source_invalid")
                    reported = _snapshot({**after, "history": result.get("history_status", {})})["history"]
                    history = dict(self._saved.get("history", {}))
                    for alias, status in reported.items():
                        previous = history.get(alias)
                        if previous and previous["status"] == "incomplete":
                            status = {"status": "incomplete", "reasons": list(dict.fromkeys(previous["reasons"] + status["reasons"]))}
                        history[alias] = status
                    after["history"] = history
                _advance(self._saved, after)
                followed = {category.kind for category in self.follow_directory.live}
                observations = [o for o in self._observations(result["rows"])
                                if o.target_ref not in self._absent and o.kind not in followed]
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

    def batch_ready(self, cursor):
        """Core may freeze only after the frozen range has reached its own ledger."""
        if not self.fixed_windows:
            return True
        if self._saved is None or not window_complete(self._saved):
            return False
        state = self.store.state(self.inbox.key)
        return self.inbox.offset(state, cursor) == state["sequence"]

    def history_status(self, conversation):
        return (self._saved or {}).get("history", {}).get(conversation)

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
