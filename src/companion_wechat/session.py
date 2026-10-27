"""Continuous authorized admission with shared private quotas and group windows."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta

from companion_core import delivery
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_policy import Participation

from .contracts import require
from .role import RoleSession


class RuntimeSession(RoleSession):
    def __init__(self, store, account, *, family=False, clock=None, max_age_seconds=300):
        super().__init__(store, account, duration_seconds=600, clock=clock)
        require(type(family) is bool and 30 <= max_age_seconds <= 3600, "wechat_module_configuration")
        self.family, self.max_age = family, max_age_seconds
        self.key = stable_key("wechat-runtime-session-v1", account)
        self.permissions = ReplyAuthorizations(store)
        self.participation = Participation(store, clock=self._now)

    def reply_scope(self, record, conversation):
        binding = record["bindings"][conversation]
        return ReplyScope("wechat", record["identity"], binding["kind"], binding["target_ref"])

    def _bindings(self, db=None):
        receiver = self.receiver
        require(receiver is not None and receiver.bindings is not None and receiver.connector is not None,
                "wechat_role_not_ready")
        registry = receiver.bindings
        state = self.store.state(registry.key, None, db)
        require(isinstance(state, dict) and state.get("identity") == registry.identity, "wechat_role_scope")
        values = state.get("conversations")
        require(isinstance(values, dict) and values, "wechat_role_scope")
        expected = {alias: asdict(registry.get(alias, db=db)) for alias in values}
        require(all(v["enabled"] and v["kind"] in {"private", "group"} for v in expected.values()), "wechat_role_scope")
        owner_key = "wechat-db-owner:" + stable_key("identity", registry.identity)
        require(self.store.state(owner_key, None, db) == self.account, "wechat_receiver_owner_conflict")
        connection = receiver.connection()
        require(connection.ready and connection.account_identity == registry.identity, "wechat_disconnected")
        return expected, connection

    def _permission_current(self, admission, db=None):
        scope, version = admission.get("reply_scope"), admission.get("reply_version")
        return ("ticket" in admission and isinstance(scope, dict) and isinstance(version, str)
                and self.permissions.version(ReplyScope(**scope), db=db) == version)

    def validate_run(self, run):
        first = self._generation is None
        super().validate_run(run)
        if first:
            # Fresh private admissions may resume after an explicit restart;
            # group participation always requires a fresh wake-up in the new run.
            with self.store.lock, self.store.db() as db:
                record = self._record(db)
                for admission in record["admissions"].values():
                    ticket = admission.get("ticket") or {}
                    if ticket.get("scope", {}).get("kind") == "private":
                        ticket["generation"] = run["generation"]
                self.store.set_state(self.key, record, db)

    def prepare(self):
        old = self.store.state(self.key, {})
        super().prepare()
        with self.store.lock, self.store.db() as db:
            record = self._record(db)
            record["deadline_at"] = "9999-12-31T00:00:00+00:00"  # Core owns explicit duration and stop.
            record["family"] = self.family
            if all(old.get(k) == record[k] for k in ("identity", "epoch", "bindings", "family")):
                record["admissions"] = old.get("admissions", {})
                record["pending_order"] = old.get("pending_order", list(record["admissions"]))
            self._prune(record, db)
            self.store.set_state(self.key, record, db)

    def _candidate(self, event, record):
        if not isinstance(event, Event) or (event.platform, event.account) != ("wechat", self.account):
            return False
        if event.conversation not in record["bindings"]:
            return False
        binding = record["bindings"][event.conversation]
        if binding["kind"] == "group" and not self.family:
            return False
        if self.permissions.version(self.reply_scope(record, event.conversation)) is None:
            return False
        if (event.visibility == "private" and event.actor !=
                ActorRef("wechat", binding["peer_ref"], self.account)):
            return False
        if event.actor.scope != self.account:
            return False
        meta = event.metadata.get("wechat", {})
        if (event.visibility != binding["kind"] or meta.get("identity_quality") != "verified"
                or meta.get("target_ref") != binding["target_ref"] or meta.get("binding_revision") != binding["revision"]
                or meta.get("epoch") != record["epoch"] or meta.get("time_basis") != "server"):
            return False
        try:
            occurred = datetime.fromisoformat(event.occurred_at)
            floor = self._now() - timedelta(seconds=self.max_age)
            if event.id not in record["admissions"]:
                floor = max(floor, datetime.fromisoformat(record["started_at"]) - timedelta(seconds=3))
            return floor <= occurred <= self._now() + timedelta(seconds=30)
        except (TypeError, ValueError, OverflowError):
            return False

    def _prune(self, record, db):
        for key, admission in list(record["admissions"].items()):
            if (self.store.state("core-processed:" + key, None, db) == admission["version"]
                    or not self._permission_current(admission, db)
                    or datetime.fromisoformat(admission["occurred_at"]) < self._now() - timedelta(seconds=self.max_age)):
                del record["admissions"][key]
        record["pending_order"] = [key for key in record.get("pending_order", list(record["admissions"]))
                                   if key in record["admissions"]]

    def admit(self, incoming, *, fresh_ids, db):
        record, run = self._active(db)
        self._prune(record, db)
        for event in incoming:
            if event.id in fresh_ids and self._candidate(event, record):
                ticket = None
                if event.visibility == "private":
                    ticket = self.participation.admit(self.reply_scope(record, event.conversation), event.id,
                                                      "direct", run["generation"])
                    if ticket is None:
                        continue
                record["admissions"][event.id] = {"event_id": event.id, "version": self._source_event(event, db),
                    "ticket": ticket, "generation": run["generation"],
                    "occurred_at": event.occurred_at,
                    "reply_scope": asdict(self.reply_scope(record, event.conversation)),
                    "reply_version": self.permissions.version(self.reply_scope(record, event.conversation), db=db)}
                record["pending_order"].append(event.id)
        require(len(record["admissions"]) <= 2048, "wechat_runtime_backpressure")
        self.store.set_state(self.key, record, db)

    def check_event(self, event, *, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.check_event(event, db=connection)
        record, run = self._active(db)
        require(self._candidate(event, record), "wechat_role_not_admitted")
        admission = record["admissions"].get(event.id)
        require(isinstance(admission, dict) and isinstance(admission.get("ticket"), dict), "wechat_role_not_admitted")
        self.permissions.check(self.reply_scope(record, event.conversation), admission.get("reply_version"), db=db)
        self.participation.check(admission["ticket"], run["generation"], db=db)
        require(self._source_event(event, db) == admission["version"], "wechat_role_source_changed")

    @contextmanager
    def submission_guard(self, request):
        # Shared store lock serializes management changes and the final submit.
        # No model call, database receipt wait or other await belongs in here.
        with self.store.lock:
            self.check_request(request)
            yield

    def reserve(self, request, *, db=None):
        # The encrypted sender intent and core action dedupe enforce one submit
        # per event. Shared participation reserves the applicable reply quota.
        self.check_request(request, db=db)
        with self.store.db() as connection:
            record, run = self._active(connection)
            row = connection.execute("SELECT body FROM core_actions WHERE id=?", (request.action_id,)).fetchone()
            event = Event.from_dict(self.store.ledger_decode("core_actions.body", row["body"])["event"])
            self.participation.reserve(record["admissions"][event.id]["ticket"], run["generation"], request.action_id)

    def admitted_pending(self):
        record, run = self._active()
        result, selected_threads = [], set()
        batch_size = self.store.read("companion")["data"].get("inbox_batch_size", 3)
        with self.store.lock, self.store.db() as db:
            for key in record.get("pending_order", list(record["admissions"])):
                if len(result) >= batch_size:
                    break
                admission = record["admissions"][key]
                if not self._permission_current(admission, db):
                    continue
                key, version = admission["event_id"], admission["version"]
                if self.store.state("core-processed:" + key, None, db) == version:
                    continue
                row = db.execute("SELECT body,deleted FROM core_events WHERE id=?", (key,)).fetchone()
                if row is None or row["deleted"]:
                    continue
                event = Event.from_dict(self.store.ledger_decode("core_events.body", row["body"]))
                if event.thread in selected_threads:
                    continue
                if not self._candidate(event, record):
                    continue
                if admission["ticket"] is None:
                    if admission.get("generation") != run["generation"]:
                        continue
                    # Admit one group decision at a time, in receive order.
                    # A later @ must not replace an earlier pending window.
                    admission["ticket"] = self.participation.admit(self.reply_scope(record, event.conversation),
                        event.id, event.metadata["wechat"]["trigger"], run["generation"])
                    if admission["ticket"] is None:
                        admission["generation"] = None  # Never reconsider a pre-wake message.
                        continue
                if not self.participation.eligible(admission["ticket"], run["generation"]):
                    continue
                self.store.set_state(self.key, record, db)
                self.check_event(event, db=db)
                selected_threads.add(event.thread)
                if delivery.eligible(self.core, event, version, run):
                    result.append(event)
            self.store.set_state(self.key, record, db)
        return result

    def public_status(self):
        record = self.store.state(self.key, {})
        authorized = {alias: bool(self.permissions.version(self.reply_scope(record, alias)))
                      for alias in record.get("bindings", {})}
        return {"mode": "continuous", "family_enabled": self.family and authorized.get("family", False),
                "reply_authorized": authorized, "default_allow": False,
                "status": record.get("status", "new"), "pending_admissions": len(record.get("admissions", {}))}
