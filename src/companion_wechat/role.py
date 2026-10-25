"""Explicit, bounded role-test admission over the existing receive-only stream.

The source account, enrollment, queue epoch, bindings and source checkpoint are
reused. A fresh test nonce grants no authority by itself: only newly committed
source events from the verified owner can be admitted, once per bound target.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from filelock import FileLock, Timeout

from companion_core import delivery, events
from companion_core.contracts import ActorRef, Capability, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.orchestration import Stage, Workflow
from companion_core.runtime_keys import RUN_KEYS
from companion_core.schema import obj

from .connector import WeChatConnector
from .contracts import SendRequest, digest, identifier, require

_ALIASES = ("owner_main", "family")


class RoleSession:
    """One explicitly started test, with at most one event/action per target."""

    def __init__(self, store, account, *, duration_seconds=180, clock=None, nonce_factory=None):
        require(identifier(account) and type(duration_seconds) is int and 5 <= duration_seconds <= 600,
                "wechat_role_configuration")
        self.store, self.account = store, account
        self.duration_seconds = duration_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(12).upper())
        self.key = stable_key("wechat-role-session-v1", account)
        # This is a process-lifetime lease, not a per-thread mutation mutex.
        # Runtime/host guards also run in worker threads and must see the same
        # held lease. store.lock still provides thread-local write exclusion.
        self._lease = FileLock(str(store.runtime / (stable_key("wechat-collector", account) + ".lock")),
                               timeout=0, thread_local=False)
        self.receiver = self.core = None
        self._id = self._nonce = self._generation = None
        self._closed = False

    def __repr__(self):
        return "RoleSession(<redacted>)"

    @property
    def nonce(self):
        require(self._nonce is not None, "wechat_role_not_ready")
        return self._nonce

    def _now(self):
        value = self.clock()
        require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
                "wechat_role_configuration")
        return value.astimezone(timezone.utc)

    def bind(self, receiver, core):
        require(self.core is None and receiver.store is self.store and core.store is self.store
                and receiver.account == self.account, "wechat_role_scope")
        self.receiver, self.core = receiver, core

    def _bindings(self, db=None):
        receiver = self.receiver
        require(receiver is not None and receiver.bindings is not None and receiver.connector is not None,
                "wechat_role_not_ready")
        registry = receiver.bindings
        state = self.store.state(registry.key, None, db)
        require(isinstance(state, dict) and state.get("identity") == registry.identity,
                "wechat_role_scope")
        values = state.get("conversations")
        require(isinstance(values, dict) and set(values) == set(_ALIASES), "wechat_role_scope")
        expected = {alias: asdict(registry.get(alias, db=db)) for alias in _ALIASES}
        private, group = expected["owner_main"], expected["family"]
        require(private["kind"] == "private" and group["kind"] == "group"
                and private["enabled"] and group["enabled"]
                and group["members"].get(private["peer_ref"]) == private["peer_ref"], "wechat_role_scope")
        owner_key = "wechat-db-owner:" + stable_key("identity", registry.identity)
        require(self.store.state(owner_key, None, db) == self.account, "wechat_receiver_owner_conflict")
        connection = receiver.connection()
        require(connection.ready and connection.account_identity == registry.identity, "wechat_disconnected")
        return expected, connection

    def prepare(self):
        """Called by core.start through validate_start; does not start a worker."""
        require(not self._closed and self.core is not None, "wechat_role_not_ready")
        if self._id is not None:
            self._record()
            return
        try:
            self._lease.acquire()
        except Timeout:
            raise AssistantError("wechat_role_collector_busy", "同一微信收件账号已有运行者") from None
        try:
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                old_run = self.store.state(self.core.run_key, {}, db)
                require(old_run.get("status") != "active", "wechat_role_already_active")
                self._other_runs_quiet(db)
                bindings, connection = self._bindings(db)
                token = self.nonce_factory()
                require(type(token) is str and re.fullmatch(r"[A-F0-9]{24}", token) is not None,
                        "wechat_role_configuration")
                now = self._now()
                self._id, self._nonce = secrets.token_hex(16), "XLD-ROLE-" + token
                self.store.set_state(self.key, {
                    "id": self._id, "nonce": self._nonce, "status": "prepared", "generation": None,
                    "started_at": now.isoformat(), "deadline_at": (now + timedelta(seconds=self.duration_seconds)).isoformat(),
                    "identity": connection.account_identity, "epoch": connection.epoch, "bindings": bindings,
                    "admissions": {}, "reservations": {},
                }, db)
        except BaseException:
            self._id = self._nonce = None
            self._lease.release()
            raise

    def _other_runs_quiet(self, db):
        for key in RUN_KEYS:
            if key == self.core.run_key:
                continue
            run = self.store.state(key, {}, db)
            require(isinstance(run, dict), "wechat_role_scope")
            if run.get("status") != "active":
                continue
            accounts = run.get("accounts", [])
            require(isinstance(accounts, list), "wechat_role_scope")
            require(key != "core-run:wechat" and not any(
                isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == "wechat"
                for pair in accounts), "wechat_role_already_active")

    def _record(self, db=None):
        require(self._id is not None and not self._closed and self._lease.is_locked, "wechat_role_not_ready")
        record = self.store.state(self.key, None, db)
        require(isinstance(record, dict) and record.get("id") == self._id
                and record.get("nonce") == self._nonce and record.get("status") in {"prepared", "active"},
                "wechat_role_revoked")
        require(self._now() < datetime.fromisoformat(record["deadline_at"]), "wechat_role_expired")
        return record

    def validate_run(self, run):
        """Called by core.guard, without recursively invoking that guard."""
        require(self.core is not None and run.get("status") == "active"
                and run.get("accounts") == [["wechat", self.account]], "wechat_role_scope")
        with self.store.lock, self.store.db() as db:
            record = self._record(db)
            self._other_runs_quiet(db)
            current = self.store.state(self.core.run_key, {}, db)
            require(current.get("status") == "active" and current.get("generation") == run.get("generation"),
                    "wechat_role_revoked")
            generation = run.get("generation")
            require(identifier(generation) and self._generation in (None, generation), "wechat_role_generation")
            bindings, connection = self._bindings(db)
            require(bindings == record["bindings"] and connection.account_identity == record["identity"]
                    and connection.epoch == record["epoch"], "wechat_role_binding_changed")
            if self._generation is None:
                require(record["generation"] is None, "wechat_role_generation")
                record.update(generation=generation, status="active")
                self.store.set_state(self.key, record, db)
                self._generation = generation
            require(record["generation"] == generation, "wechat_role_generation")

    def _active(self, db=None):
        require(self.core is not None, "wechat_role_not_ready")
        run = self.core.guard()
        record = self._record(db)
        require(record["status"] == "active" and record["generation"] == run["generation"], "wechat_role_generation")
        return record, run

    def _candidate(self, event, record):
        if not isinstance(event, Event) or (event.platform, event.account) != ("wechat", self.account):
            return False
        binding = record["bindings"].get(event.conversation)
        owner = record["bindings"]["owner_main"]["peer_ref"]
        if binding is None or event.actor != ActorRef("wechat", owner, self.account):
            return False
        meta = event.metadata.get("wechat", {})
        if (event.visibility != binding["kind"] or meta.get("identity_quality") != "verified"
                or meta.get("target_ref") != binding["target_ref"] or meta.get("binding_revision") != binding["revision"]
                or meta.get("epoch") != record["epoch"] or meta.get("time_basis") != "server"):
            return False
        if not re.match(r"^" + re.escape(record["nonce"]) + r"(?:\s|$)", event.text.strip()):
            return False
        try:
            occurred = datetime.fromisoformat(event.occurred_at)
            if occurred.tzinfo is None or occurred.utcoffset() is None:
                return False
            # The source's timestamp is integral seconds. The unguessable nonce
            # additionally separates this test from earlier messages in that second.
            floor = datetime.fromisoformat(record["started_at"]).replace(microsecond=0)
            return floor <= occurred <= self._now() + timedelta(seconds=30)
        except (TypeError, ValueError, OverflowError):
            return False

    def _source_event(self, event, db):
        reservation = self.store.state(stable_key("wechat-observation-v1", self.account, event.native_id), None, db)
        require(isinstance(reservation, dict) and digest(reservation.get("event")) == digest(event.as_dict()),
                "wechat_event_untrusted")
        row = db.execute("SELECT body,version,deleted FROM core_events WHERE id=?", (event.id,)).fetchone()
        require(row is not None and not row["deleted"]
                and digest(self.store.ledger_decode("core_events.body", row["body"])) == digest(event.as_dict()),
                "wechat_event_revoked")
        return row["version"]

    def admit(self, incoming, *, fresh_ids, db):
        """Called in the same transaction as source event/cursor persistence."""
        record, _run = self._active(db)
        changed = False
        for event in incoming:
            if not self._candidate(event, record):
                continue
            old = record["admissions"].get(event.conversation)
            if old is not None or event.id not in fresh_ids:
                continue
            version = self._source_event(event, db)
            record["admissions"][event.conversation] = {"event_id": event.id, "version": version}
            changed = True
        if changed:
            self.store.set_state(self.key, record, db)

    def check_event(self, event, *, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.check_event(event, db=connection)
        record, _run = self._active(db)
        require(self._candidate(event, record), "wechat_role_not_admitted")
        admission = record["admissions"].get(event.conversation)
        require(isinstance(admission, dict) and admission["event_id"] == event.id, "wechat_role_not_admitted")
        require(self._source_event(event, db) == admission["version"], "wechat_role_source_changed")

    def _request_record(self, request, db):
        require(isinstance(request, SendRequest), "wechat_role_request_invalid")
        record, run = self._active(db)
        require(not run["read_only"] and request.generation == record["generation"]
                and request.account_identity == record["identity"] and request.epoch == record["epoch"],
                "wechat_role_request_invalid")
        binding = record["bindings"].get(request.conversation)
        require(binding is not None and (request.target_ref, request.revision) ==
                (binding["target_ref"], binding["revision"]), "wechat_role_request_invalid")
        row = db.execute("SELECT * FROM core_actions WHERE id=?", (request.action_id,)).fetchone()
        require(row is not None and row["status"] == "dispatching" and row["platform"] == "wechat"
                and row["account"] == self.account and row["generation"] == request.generation
                and row["capability"] == "message.reply", "wechat_action_missing")
        body = self.store.ledger_decode("core_actions.body", row["body"])
        require(body["arguments"] == {"text": request.text}, "wechat_action_mismatch")
        event = Event.from_dict(body["event"])
        require(event.conversation == request.conversation, "wechat_role_request_invalid")
        require(row["dedupe"] == stable_key("dispatch", "wechat", self.account, event.id, "message.reply"),
                "wechat_action_mismatch")
        self.check_event(event, db=db)
        require(record["reservations"].get(request.conversation) in (None, request.action_id), "wechat_role_reply_limit")
        return record

    def check_request(self, request, *, db=None):
        """Pure, repeatable validation for prepare/commit guards."""
        if db is None:
            with self.store.db() as connection:
                return self.check_request(request, db=connection)
        self._request_record(request, db)

    def reserve(self, request, *, db=None):
        """Freeze one action per target; join the sender's durable intent transaction."""
        if db is None:
            with self.store.lock, self.store.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.reserve(request, db=connection)
        record = self._request_record(request, db)
        if request.conversation not in record["reservations"]:
            record["reservations"][request.conversation] = request.action_id
            self.store.set_state(self.key, record, db)

    def admitted_pending(self):
        record, run = self._active()
        pending = []
        for alias in _ALIASES:
            admission = record["admissions"].get(alias)
            if admission is None:
                continue
            with self.store.db() as db:
                if self.store.state("core-processed:" + admission["event_id"], None, db) == admission["version"]:
                    continue
                row = db.execute("SELECT body,deleted FROM core_events WHERE id=?", (admission["event_id"],)).fetchone()
                require(row is not None and not row["deleted"], "wechat_event_revoked")
                event = Event.from_dict(self.store.ledger_decode("core_events.body", row["body"]))
            self.check_event(event)
            if delivery.eligible(self.core, event, admission["version"], run):
                pending.append(event)
        return pending

    def public_status(self):
        record = self.store.state(self.key, {}) if self._id is not None else {}
        ours = record.get("id") == self._id and self._id is not None
        completed = {key: 0 for key in _ALIASES}
        if ours:
            with self.store.db() as db:
                for alias, action_id in record.get("reservations", {}).items():
                    if alias in completed:
                        row = db.execute("SELECT status FROM core_actions WHERE id=?", (action_id,)).fetchone()
                        completed[alias] = int(row is not None and row["status"] == "succeeded")
        return {"status": record.get("status", "new") if ours else "new",
                "nonce": self._nonce, "deadline_at": record.get("deadline_at") if ours else None,
                "admitted": {key: int(ours and key in record.get("admissions", {})) for key in _ALIASES},
                "reserved": {key: int(ours and key in record.get("reservations", {})) for key in _ALIASES},
                "completed": completed}

    def remaining_seconds(self):
        """Wall-clock budget without core guards, mutation, or source reads."""
        if self._id is None or self._closed:
            return 0.0
        record = self.store.state(self.key, {})
        if record.get("id") != self._id or record.get("status") not in {"prepared", "active"}:
            return 0.0
        return max(0.0, (datetime.fromisoformat(record["deadline_at"]) - self._now()).total_seconds())

    def revoke_generation(self, generation):
        if self._id is None or generation not in (self._generation, None):
            return
        with self.store.lock, self.store.db() as db:
            record = self.store.state(self.key, {}, db)
            if record.get("id") == self._id:
                record["status"] = "revoked"
                self.store.set_state(self.key, record, db)

    def close(self):
        if self._closed:
            return
        try:
            self.revoke_generation(self._generation)
        finally:
            self._closed = True
            self._lease.release()


class RoleAdapter(WeChatConnector):
    """Only explicit admitted events may enter core.handle or send preparation."""

    def __init__(self, receiver, sender, permits, session):
        require(receiver.connector is not None and receiver.bindings is not None, "wechat_role_not_ready")
        require(session.store is receiver.store and session.account == receiver.account, "wechat_role_scope")
        self.receiver, self.session = receiver, session
        self._closed = False
        super().__init__(receiver.bindings, receiver, permits, sender=sender, include_untriggered_group=True,
                         verified_sender_ids=receiver.connector.normalizer.verified_sender_ids)

    def bind_runtime(self, core):
        super().bind_runtime(core)
        self.session.bind(self.receiver, core)

    def validate_start(self):
        self.sender.preflight()
        self.session.prepare()

    def capabilities(self):
        return [Capability("message.reply", "短测：仅向本次已批准会话回复单行纯文本，最多600字符", obj({
            "text": {"type": "string", "minLength": 1, "maxLength": 600, "pattern": r"^[^\r\n]+$"}}))]

    def validate_run(self, run):
        self.session.validate_run(run)

    async def receive(self, cursor):
        self.core.guard()
        status = await self.receiver.pump_once()
        if status.get("state") == "blocked":
            raise AssistantError("wechat_role_source_blocked", "微信来源需要人工检查")
        incoming, next_cursor = await super().receive(cursor)
        key = stable_key("cursor", self.platform, self.account)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.core.guard()
            saved = self.store.state(key, None, db)
            require((saved is None and (not cursor or set(cursor) <= {"since"})) or saved == cursor,
                    "wechat_cursor_invalid")
            fresh = set()
            for event in incoming:
                require((event.platform, event.account) == (self.platform, self.account), "wechat_role_scope")
                receipt = events.ingest(self.store, event)
                require(not receipt.get("changed"), "wechat_event_conflict")
                if receipt["new"]:
                    fresh.add(event.id)
            self.session.admit(incoming, fresh_ids=fresh, db=db)
            self.store.set_state(key, next_cursor, db)
        return self.admitted_pending(), next_cursor

    async def collect_once(self):
        cursor = self.store.state(stable_key("cursor", self.platform, self.account), {})
        incoming, _cursor = await self.receive(cursor)
        return incoming

    def admitted_pending(self):
        return self.session.admitted_pending()

    def validate_event(self, event):
        context = super().validate_event(event)
        self.session.check_event(event)
        return context

    def workflow(self, core):
        require(core is self.core, "wechat_role_scope")

        async def collect(state):
            state["events"] = await self.collect_once()

        async def respond(state):
            for event in state["events"]:
                self.validate_event(event)
                if not core.remaining_model_calls():
                    break
                await core.handle(event)

        return Workflow("wechat", self.account, (
            Stage("inbox", collect, 10, "prepare"),
            Stage("approved_replies", respond, 20, requires_model=True),
        ))

    def runtime_stopped(self, generation):
        self.session.revoke_generation(generation)
        super().runtime_stopped(generation)

    async def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.runtime_stopped(self.session._generation)
        finally:
            try:
                await self.sender.close()
            finally:
                try:
                    await self.receiver.close()
                finally:
                    self.session.close()
