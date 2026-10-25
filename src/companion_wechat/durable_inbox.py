"""Persistent receive queue for trusted observations, independent of UI/DB producers.

Uses the core's encrypted state ledger in PostgreSQL. Does not open WeChat, infer
identities, produce observations from screenshots, or implement a send channel.
"""
import json
import re
import uuid
from dataclasses import asdict

from companion_core.contracts import stable_key

from .contracts import Batch, Observation, identifier, require


def _checkpoint_json(value, *, optional=False):
    """Canonical bounded JSON for type-sensitive CAS, detached from caller data."""
    if optional and value is None:
        return None
    require(isinstance(value, dict), "wechat_checkpoint_invalid")
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        require(len(rendered.encode("utf-8")) <= 65536, "wechat_checkpoint_invalid")
        return rendered
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        require(False, "wechat_checkpoint_invalid")


class DurableInbox:
    """An account/ingestion-epoch queue with independently retained receipts.

    Pending observations contain their original payload until acknowledged.
    Permanent per-ID receipts retain only fingerprints, so acknowledgment cannot
    resurrect a row and lifetime receipt count cannot fill the pending queue.
    All values use store.state/set_state, including PostgreSQL ledger encryption.
    """

    def __init__(self, store, account_identity, epoch=None, *, capacity=200):
        require(type(capacity) is int and 1 <= capacity <= 500)
        require(identifier(account_identity) and (epoch is None or identifier(epoch)))
        self.store, self.account, self.capacity = store, account_identity, capacity
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if epoch is None:
                epoch_key = "wechat-ingestion:" + stable_key("account", account_identity)
                saved = store.state(epoch_key, db=db)
                if saved is None:
                    saved = {"epoch": uuid.uuid4().hex}
                    store.set_state(epoch_key, saved, db)
                require(isinstance(saved, dict) and identifier(saved.get("epoch")), "wechat_inbox_invalid")
                epoch = saved["epoch"]
            self.epoch = epoch
            self.key = "wechat-inbox:" + stable_key("stream", account_identity, epoch)
            state = store.state(self.key, db=db)
            if state is None:
                store.set_state(self.key, {"version": 2, "stream": uuid.uuid4().hex, "sequence": 0,
                    "acknowledged": 0, "offered": 0, "pending": []}, db)
            elif "seen" in state:
                # Upgrade existing queues atomically without losing receipts,
                # outstanding payloads, stream identity, or acknowledged offsets.
                for observation_id, fingerprint in state["seen"].items():
                    require(isinstance(observation_id, str) and re.fullmatch(r"[a-f0-9]{32}", observation_id)
                            and isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint),
                            "wechat_inbox_invalid")
                    key = self._receipt_key(observation_id)
                    previous = store.state(key, db=db)
                    require(previous is None or previous == fingerprint, "wechat_observation_changed")
                    store.set_state(key, fingerprint, db)
                del state["seen"]
                state["version"] = 2
                store.set_state(self.key, state, db)

    def _receipt_key(self, observation_id):
        return self.key + ":receipt:" + observation_id

    def _checkpoint_key(self, checkpoint_key):
        require(identifier(checkpoint_key), "wechat_checkpoint_invalid")
        return self.key + ":checkpoint:" + stable_key("source", checkpoint_key)

    def checkpoint(self, checkpoint_key):
        """Return the committed opaque source checkpoint, or None before baseline."""
        value = self.store.state(self._checkpoint_key(checkpoint_key))
        _checkpoint_json(value, optional=True)
        return value

    def _enqueue(self, observations, db):
        state = self.store.state(self.key, db=db)
        fresh = {}
        for observation in observations:
            require(isinstance(observation, Observation))
            require((observation.account_identity, observation.epoch) == (self.account, self.epoch),
                    "wechat_stale_connection")
            fingerprint = observation.fingerprint
            previous = self.store.state(self._receipt_key(observation.observation_id), db=db)
            if previous is not None:
                require(previous == fingerprint, "wechat_observation_changed")
            elif observation.observation_id in fresh:
                require(fresh[observation.observation_id][1] == fingerprint, "wechat_observation_changed")
            else:
                fresh[observation.observation_id] = (observation, fingerprint)
        require(len(state["pending"]) + len(fresh) <= self.capacity, "wechat_inbox_full")
        for observation, _ in fresh.values():
            state["sequence"] += 1
            state["pending"].append([state["sequence"], asdict(observation)])
        require(len(json.dumps(state, ensure_ascii=False).encode("utf-8")) <= 2_000_000, "wechat_inbox_full")
        for observation_id, (_, fingerprint) in fresh.items():
            self.store.set_state(self._receipt_key(observation_id), fingerprint, db)
        if fresh:
            self.store.set_state(self.key, state, db)
        return len(fresh)

    def publish(self, observation):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            return bool(self._enqueue((observation,), db))

    def publish_batch(self, observations, *, checkpoint_key, expected_checkpoint, checkpoint):
        """Atomically CAS a source checkpoint and enqueue every new observation.

        Returns the number of newly queued IDs. Empty batches establish a baseline
        or checkpoint filtered rows; duplicate IDs never reappear after ack. A
        failed CAS, full pending queue, content conflict, or storage failure rolls
        back all receipts, queued observations, and the checkpoint together.
        """
        require(isinstance(observations, (list, tuple)) and len(observations) <= 500)
        key = self._checkpoint_key(checkpoint_key)
        expected = _checkpoint_json(expected_checkpoint, optional=True)
        updated = _checkpoint_json(checkpoint)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            actual = _checkpoint_json(self.store.state(key, db=db), optional=True)
            require(actual == expected, "wechat_checkpoint_conflict")
            count = self._enqueue(observations, db)
            self.store.set_state(key, json.loads(updated), db)
            return count

    @staticmethod
    def offset(state, cursor):
        require(isinstance(cursor, dict), "wechat_cursor_invalid")
        if not cursor or set(cursor) <= {"since"}:
            require(state["acknowledged"] == 0, "wechat_cursor_regressed")
            return 0
        require(set(cursor) == {"wechat_stream", "offset"} and cursor["wechat_stream"] == state["stream"],
                "wechat_cursor_stream")
        value = cursor["offset"]
        require(type(value) is int and state["acknowledged"] <= value <= state["offered"], "wechat_cursor_invalid")
        return value

    async def acknowledge(self, cursor):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store.state(self.key, db=db)
            offset = self.offset(state, cursor)
            state["pending"] = [row for row in state["pending"] if row[0] > offset]
            state["acknowledged"] = offset
            self.store.set_state(self.key, state, db)

    async def poll(self, cursor, *, limit=100):
        require(type(limit) is int and 1 <= limit <= 100)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store.state(self.key, db=db)
            offset = self.offset(state, cursor)
            selected = [row for row in state["pending"] if row[0] > offset][:limit]
            end = selected[-1][0] if selected else offset
            state["offered"] = max(state["offered"], end)
            self.store.set_state(self.key, state, db)
            return Batch(tuple(Observation(**row[1]) for row in selected),
                         {"wechat_stream": state["stream"], "offset": end})
