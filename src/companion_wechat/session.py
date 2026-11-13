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
        from companion_core.merged_inbox import MergedInbox

        self.merged = MergedInbox(store)
        self._page_count = 0

    def _hydrate(self, record, db):
        if "admission_pages" not in record:
            return record
        count = record["admission_pages"]
        require(type(count) is int and count >= 0, "wechat_role_scope")
        admissions, order = {}, []
        for index in range(count):
            page = self.store.state(f"{self.key}:admissions:{index}", None, db)
            require(isinstance(page, list) and 0 < len(page) <= 128, "wechat_role_scope")
            for key, admission in page:
                require(key not in admissions and admission["event_id"] == key, "wechat_role_scope")
                admissions[key] = admission
                order.append(key)
        require(len(order) == record["admission_count"], "wechat_role_scope")
        self._page_count = count
        return {k: v for k, v in record.items() if k not in {"admission_pages", "admission_count"}} | {
            "admissions": admissions, "pending_order": order}

    def _record(self, db=None):
        if db is None:
            with self.store.lock, self.store.db() as connection:
                return self._record(connection)
        with self.store.lock:
            return self._hydrate(super()._record(db), db)

    def _save_record(self, record, db):
        # Lifecycle-only updates may pass the compact root through unchanged.
        if "admission_pages" in record:
            return super()._save_record(record, db)
        order = record.get("pending_order", list(record["admissions"]))
        require(len(order) == len(record["admissions"]) and set(order) == set(record["admissions"]),
                "wechat_role_scope")
        if len(order) <= 128:
            for index in range(self._page_count):
                db.execute("DELETE FROM state WHERE key=?", (f"{self.key}:admissions:{index}",))
            super()._save_record(record, db)
            self._page_count = 0
            return
        count = (len(order) + 127) // 128
        for index in range(count):
            self.store.set_state(f"{self.key}:admissions:{index}",
                [[key, record["admissions"][key]] for key in order[index * 128:(index + 1) * 128]], db)
        for index in range(count, self._page_count):
            db.execute("DELETE FROM state WHERE key=?", (f"{self.key}:admissions:{index}",))
        root = {k: v for k, v in record.items() if k not in {"admissions", "pending_order"}}
        root.update(admissions={}, admission_pages=count, admission_count=len(order))
        super()._save_record(root, db)
        self._page_count = count

    def reply_scope(self, record, conversation):
        binding = record["bindings"][conversation]
        return ReplyScope("wechat", record["identity"], binding["kind"], binding["target_ref"])

    def bind(self, receiver, core):
        super().bind(receiver,core)
        receiver.binding_added = self._add_binding

    def _add_binding(self, binding, db):
        record = self._record(db)
        record["bindings"][binding.key] = asdict(binding)
        self._save_record(record,db)

    def _bindings(self, db=None):
        receiver = self.receiver
        require(receiver is not None and receiver.bindings is not None and receiver.connector is not None,
                "wechat_role_not_ready")
        registry = receiver.bindings
        state = self.store.state(registry.key, None, db)
        require(isinstance(state, dict) and state.get("identity") == registry.identity, "wechat_role_scope")
        values = state.get("conversations")
        from .bindings import ConversationBinding
        require(isinstance(values, dict), "wechat_role_scope")
        expected = {alias: asdict(ConversationBinding(**value)) for alias,value in values.items()}
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
                self._save_record(record, db)

    def prepare(self):
        with self.store.lock, self.store.db() as db:
            old = self._hydrate(self.store.state(self.key, {}, db), db)
        super().prepare()
        with self.store.lock, self.store.db() as db:
            record = self._record(db)
            record["deadline_at"] = "9999-12-31T00:00:00+00:00"  # Core owns explicit duration and stop.
            record["family"] = self.family
            if all(old.get(k) == record[k] for k in ("identity", "epoch", "bindings", "family")):
                record["admissions"] = old.get("admissions", {})
                record["pending_order"] = old.get("pending_order", list(record["admissions"]))
            self._prune(record, db)
            self._save_record(record, db)

    def _candidate(self, event, record):
        if not isinstance(event, Event) or (event.platform, event.account) != ("wechat", self.account):
            return False
        if event.conversation not in record["bindings"]:
            return False
        binding = record["bindings"][event.conversation]
        if binding["kind"] == "group" and not self.family:
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
            return (occurred <= self._now() + timedelta(seconds=30)
                    and self.permissions.version(self.reply_scope(record, event.conversation),
                                                 message_time=occurred.timestamp()) is not None)
        except (TypeError, ValueError, OverflowError):
            return False

    def _prune(self, record, db):
        from companion_core.reply_audience import Audience, membership_tenure, read
        from companion_core.reply_exclusion import accepts, seconds

        for key, admission in list(record["admissions"].items()):
            if (self.store.state("core-processed:" + key, None, db) == admission["version"]
                      or self.store.state("merged-consumed:" + key, {}, db).get("version") == admission["version"]):
                del record["admissions"][key]
                continue
            if self._permission_current(admission,db):
                continue
            revision = admission.get("follow_revision")
            if revision:
                scope = ReplyScope(**admission["reply_scope"])
                # Input from before a lifted exclusion is retired instead of being
                # re-armed for catch-up, exactly like a consumed batch.
                if not accepts(self.store, scope, seconds(admission.get("occurred_at")), db=db):
                    del record["admissions"][key]
                    continue
                policy = read(self.store,Audience("wechat",scope.account_identity,scope.kind),db)
                if policy["enabled"] and policy["version"] == revision:
                    batch_key = self.store.state("merged-anchor:"+key,None,db)
                    batch = self.store.state(batch_key,None,db) if batch_key else None
                    if batch and batch["status"] == "pending":
                        terminal = self.merged.terminal(batch,db)
                        if terminal:
                            self.merged.finish(batch,terminal)
                            del record["admissions"][key]
                            continue
                    # Keep only unprocessed input. A previous send attempt, including
                    # one whose receipt was lost, must never acquire a fresh permit.
                    dedupes = [stable_key("dispatch","wechat",self.account,key,cap)
                               for cap in ("message.reply","conversation.end")]
                    if db.execute("SELECT 1 FROM core_actions WHERE dedupe IN (?,?)",dedupes).fetchone() is None:
                        admission.update(ticket=None,reply_version=self.permissions.version(scope,db=db),catchup=True,
                                         follow_tenure=membership_tenure(self.store, scope, db))
                        continue
            del record["admissions"][key]
        record["pending_order"] = [key for key in record.get("pending_order", list(record["admissions"]))
                                   if key in record["admissions"]]

    def admit(self, incoming, *, fresh_ids, db):
        from companion_core.reply_audience import Audience, membership_tenure, read
        from companion_core.reply_exclusion import accepts, seconds

        from .follow_inbox import event_scope_key
        record, run = self._active(db)
        self._prune(record, db)
        for event in incoming:
            stamp = seconds(event.occurred_at)
            scope = self.reply_scope(record, event.conversation)
            marker = self.store.state(event_scope_key(self.account,event.native_id),None,db)
            if marker:
                policy = read(self.store,Audience("wechat",record["identity"],event.visibility),db)
                if not policy["enabled"] or policy["version"] != marker["revision"]:
                    continue
                # A follow/catch-up marker never carries an excluded conversation,
                # nor input from before a lifted exclusion, into a fresh reply.
                if not accepts(self.store, scope, stamp, db=db):
                    continue
            if event.id in fresh_ids and (self._candidate(event, record) or marker):
                ticket = None
                if event.visibility == "private":
                    ticket = self.participation.admit(scope, event.id, "direct", run["generation"],
                                                      message_time=stamp)
                    if ticket is None and not marker:
                        continue
                record["admissions"][event.id] = {"event_id": event.id, "version": self._source_event(event, db),
                    "ticket": ticket, "generation": run["generation"],
                    "occurred_at": event.occurred_at, "reply_scope": asdict(scope),
                    "reply_version": self.permissions.version(scope, db=db)}
                if marker:
                    # The follow position keeps the raw membership tenure, so store the
                    # same space here; the exclusion revision only wraps reply_version.
                    record["admissions"][event.id].update(
                        follow_revision=marker["revision"], catchup=marker["catchup"],
                        follow_tenure=membership_tenure(self.store, scope, db),
                        history=marker.get("history"))
                record["pending_order"].append(event.id)
        # A fixed range drains transport pages into durable core events before
        # replying. A transport-sized cap here would deadlock long ranges.
        if not self.receiver.fixed_windows:
            require(len(record["admissions"]) <= 2048, "wechat_runtime_backpressure")
        self._save_record(record, db)

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

    def check_context_event(self, event):
        with self.store.db() as db:
            record, _run = self._active(db)
            require(self._candidate(event, record), "wechat_role_not_admitted")
            admission = record["admissions"].get(event.id)
            require(admission is not None and self._permission_current(admission, db), "reply_not_authorized")
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
        from companion_core.reply_exclusion import seconds

        cursor = self.store.state(stable_key("cursor", "wechat", self.account), {})
        if not self.receiver.batch_ready(cursor):
            return []
        record, run = self._active()
        result, groups = [], {}
        batch_size = self.store.read("companion")["data"].get("inbox_batch_size", 3)
        with self.store.lock, self.store.db() as db:
            for key in record.get("pending_order", list(record["admissions"])):
                admission = record["admissions"][key]
                if not self._permission_current(admission, db):
                    continue
                key, version = admission["event_id"], admission["version"]
                if (self.store.state("core-processed:" + key, None, db) == version
                        or self.store.state("merged-consumed:" + key, {}, db).get("version") == version):
                    continue
                row = db.execute("SELECT body,deleted FROM core_events WHERE id=?", (key,)).fetchone()
                if row is None or row["deleted"]:
                    continue
                event = Event.from_dict(self.store.ledger_decode("core_events.body", row["body"]))
                if not self._candidate(event, record):
                    continue
                groups.setdefault(event.thread, []).append(event)
            for incoming in groups.values():
                if len(result) >= batch_size:
                    break
                head = record["admissions"][incoming[0].id]
                position = self.receiver.follow.position(head["reply_scope"]["target_id"]) if head.get("follow_revision") else None
                # Compare the position's tenure with the same raw membership space it was
                # written in: a lifted exclusion wraps reply_version but is not a member
                # change, while an actual membership change or a still-open window is.
                tenure = head.get("follow_tenure", head["reply_version"])
                if head.get("follow_revision") and (not position or position["window"]
                                                    or position["tenure"] != tenure):
                    continue
                history = position["history"] if position else self.receiver.history_status(incoming[0].conversation)
                if position:
                    reasons = list((history or {}).get("reasons", []))
                    for event in incoming:
                        reasons.extend((record["admissions"][event.id].get("history") or {}).get("reasons", []))
                    if reasons:
                        history = {"status":"incomplete","reasons":list(dict.fromkeys(reasons))}
                if history is not None:
                    self.merged.record_history(incoming[0], history)
                revision = record["admissions"][incoming[0].id]["reply_version"]
                batch = self.merged.freeze(incoming, since=run["started_at"], generation=run["generation"],
                                           audience_revision=revision,force_catchup=any(record["admissions"][e.id].get("catchup") for e in incoming))
                if batch is None:
                    continue
                from companion_core import events

                event = Event.from_dict(events.get(self.store, batch["anchor"])["event"])
                admission = record["admissions"][event.id]
                if admission["ticket"] is None or not self.participation.eligible(admission["ticket"], run["generation"]):
                    scope = self.reply_scope(record, event.conversation)
                    stamp = seconds(event.occurred_at)
                    if event.visibility == "group" and batch["catchup"]:
                        if event.metadata["wechat"]["trigger"] not in {"mention", "reply"}:
                            self.merged.finish(batch, "no_trigger")
                            continue
                        admission["ticket"] = self.participation.admit_backlog(scope, event.id, run["generation"],
                                                                              batch["id"], message_time=stamp)
                    else:
                        admission["ticket"] = self.participation.admit(scope, event.id,
                            event.metadata["wechat"]["trigger"], run["generation"], message_time=stamp)
                    if admission["ticket"] is None:
                        if event.visibility == "group":
                            self.merged.finish(batch, "not_admitted")
                        continue
                self._save_record(record, db)
                self.check_event(event, db=db)
                if delivery.eligible(self.core, event, admission["version"], run, keep_pending=True):
                    result.append(event)
            self._save_record(record, db)
        return result

    def public_status(self):
        record = self.store.state(self.key, {})
        authorized = {alias: bool(self.permissions.version(self.reply_scope(record, alias)))
                      for alias in record.get("bindings", {})}
        return {"mode": "continuous", "family_enabled": self.family and authorized.get("family", False),
                "reply_authorized": authorized, "default_allow": False,
                "status": record.get("status", "new"),
                "pending_admissions": record.get("admission_count", len(record.get("admissions", {})))}
