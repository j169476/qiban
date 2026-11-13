"""Transactional intake of one frozen native history range per conversation.

The owned transport supplies verified stable bounds, never cache-local msgIds.
Receiving a page commits its events and position together. Only a completed
range is visible to reply scheduling. Transport acknowledgements follow commit.
"""
from copy import deepcopy
from datetime import datetime

from companion_core import events
from companion_core.contracts import Event, stable_key
from companion_core.history_status import validate_history_status
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, require
from companion_core.reply_exclusion import accepts

from .config import qq_id
from .native_normalization import normalize_native


class NativeInbox:
    def __init__(self, module):
        self.module, self.store = module, module.store

    def key(self, kind, target):
        require(kind in {"private", "group"}, "qq_event_scope")
        target = qq_id(target)
        field = "private_targets" if kind == "private" else "groups"
        require(target in self.module.intake_options[field], "qq_target_unverified")
        from companion_core.reply_audience import Audience, read
        policy = read(self.store, Audience("qq",self.module.account,kind))
        epoch = [policy["version"]] if policy["version"] else []
        return self._key(kind,target,epoch)

    def _key(self,kind,target,epoch):
        return "qq-native-window:" + stable_key("scope", self.module.account, kind, target, *epoch)

    def _queued_scopes(self, db):
        from companion_core.reply_audience import Audience, read
        counts = dict(db.execute("SELECT scope,COUNT(*) FROM qq_native_inbox GROUP BY scope"))
        if counts and self.module.intake_options:
            for kind, field in (("private","private_targets"),("group","groups")):
                version = read(self.store,Audience("qq",self.module.account,kind),db)["version"]
                for target in self.module.intake_options[field]:
                    key = self._key(kind,target,[version] if version else [])
                    if key in counts:
                        yield key, counts[key]


    def state(self, kind, target, *, db=None):
        return self.store.state(self.key(kind, target), None, db)

    def _bound(self, value, kind, target, identity):
        require(isinstance(value, dict), "qq_native_checkpoint_invalid")
        if kind == "group":
            require(set(value) == {"account", "group", "seq", "senderUin", "senderUid", "time"}
                    and value["account"] == self.module.account and value["group"] == target,
                    "qq_native_checkpoint_invalid")
            numeric = ("seq", "time")
        else:
            require(set(value) == {"accountUin", "accountUid", "peerUin", "peerUid", "senderUin",
                                   "senderUid", "clientSeq", "time", "random"}
                    and value["accountUin"] == self.module.account and value["accountUid"] == identity["uid"]
                    and value["peerUin"] == target and value["peerUid"] == identity["friends"][target],
                    "qq_native_checkpoint_invalid")
            numeric = ("clientSeq", "time", "random")
            author_uid = identity["uid"] if value["senderUin"] == self.module.account else identity["friends"][target]
            require(value["senderUin"] in {self.module.account, target} and value["senderUid"] == author_uid,
                    "qq_native_checkpoint_invalid")
        qq_id(value["senderUin"])
        require(isinstance(value["senderUid"], str) and value["senderUid"].startswith("u_")
                and all(isinstance(value[k], str) and value[k].isascii() and value[k].isdigit()
                        and len(value[k]) <= 20 for k in numeric)
                and int(value["time"]) > 0 and (kind != "group" or int(value["seq"]) > 0),
                "qq_native_checkpoint_invalid")
        return deepcopy(value)

    def publish(self, packet, *, identity, catchup=False):
        from .module import digest

        require(isinstance(packet, dict) and set(packet) == {
            "window_id", "kind", "target", "lower", "upper", "page", "complete", "messages", "history_status"
        }, "qq_native_page_invalid")
        kind, target = packet["kind"], packet["target"]
        key = self.key(kind, target)
        require(isinstance(identity, dict) and identity.get("uin") == self.module.account
                and isinstance(identity.get("uid"), str) and identity["uid"].startswith("u_")
                and isinstance(identity.get("friends"), dict) and isinstance(identity.get("groups"), list),
                "qq_account_mismatch")
        require((kind == "group" and target in identity["groups"])
                or (kind == "private" and target in identity["friends"]), "qq_target_unverified")
        # Catalog additions in other conversations do not invalidate this range.
        owner = {"uin": identity["uin"], "uid": identity["uid"],
                 "peer": identity["friends"][target] if kind == "private" else target}
        lower = None if packet["lower"] is None else self._bound(packet["lower"], kind, target, identity)
        upper = self._bound(packet["upper"], kind, target, identity)
        if lower is not None:
            order = "seq" if kind == "group" else "time"
            require(int(lower[order]) <= int(upper[order]), "qq_native_checkpoint_invalid")
        require(isinstance(packet["window_id"], str) and 1 <= len(packet["window_id"]) <= 128
                and type(packet["page"]) is int and packet["page"] >= 0 and type(packet["complete"]) is bool
                and isinstance(packet["messages"], list) and len(packet["messages"]) <= 100,
                "qq_native_page_invalid")
        history = validate_history_status(packet["history_status"])
        require(history is not None, "qq_native_page_invalid")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            from companion_core.reply_audience import Audience, catchup_since, read
            from companion_core.reply_exclusion import ReplyExclusions
            policy = read(self.store,Audience("qq",self.module.account,kind),db)
            since = catchup_since(policy)
            scope = ReplyScope("qq", self.module.account, kind, target)
            excluded = ReplyExclusions(self.store).record(scope, db=db)["excluded"]
            if since is not None and not excluded:
                require(ReplyAuthorizations(self.store).version(scope, db=db) is not None,
                        "reply_not_authorized")
            incoming = []
            for raw in packet["messages"]:
                require(isinstance(raw, dict) and raw.get("chatType") == (1 if kind == "private" else 2)
                        and raw.get("peerUid") == owner["peer"], "qq_event_scope")
                # Category activation is fixed for this window. Exclusion eligibility
                # is checked below, after computing the immutable page fingerprint.
                stamp = int(raw["msgTime"]) if str(raw.get("msgTime", "")).isdecimal() else None
                if since is not None and stamp is not None and stamp < since:
                    incoming.append(None)
                    continue
                event = normalize_native(raw, self.module.intake_options, identity=identity,
                    sent_reference=lambda k, t, i: self.store.state(self.module.sent_key(k, t, i), None, db) is not None)
                if event is not None:
                    order = "msgSeq" if kind == "group" else "msgTime"
                    endpoint = "seq" if kind == "group" else "time"
                    position = int(raw[order])
                    require(position <= int(upper[endpoint]) and (lower is None or
                            (position > int(lower[endpoint]) if kind == "group" else
                             position >= int(lower[endpoint]))), "qq_native_message_outside_window")
                incoming.append(event)
            # A cache rebuild changes native locators, not source identity or a
            # committed page. Compare canonical events rather than raw msgIds.
            fingerprint = digest({**packet, "messages": [event.as_dict() if event else None for event in incoming]})
            saved = self.store.state(key, None, db)
            if saved is not None and saved["window_id"] == packet["window_id"]:
                require(saved["owner"] == owner and saved["lower"] == lower and saved["upper"] == upper,
                        "qq_native_window_changed")
                if packet["page"] == saved["next_page"] - 1:
                    require(saved["last_digest"] == fingerprint, "qq_native_page_conflict")
                    return {"next_page": saved["next_page"], "complete": saved["complete"]}
                require(not saved["complete"] and packet["page"] == saved["next_page"],
                        "qq_native_page_order")
            else:
                require(packet["page"] == 0, "qq_native_page_order")
                if saved is not None:
                    require(saved["complete"] and saved["owner"] == owner and saved["upper"] == lower,
                            "qq_native_window_changed")
                    require(db.execute("SELECT 1 FROM qq_native_inbox WHERE scope=? LIMIT 1", (key,)).fetchone()
                            is None, "qq_inbox_backpressure")
                saved = {"window_id": packet["window_id"], "owner": owner, "lower": lower, "upper": upper,
                         "next_page": 0, "complete": False, "history_status": {"status": "complete", "reasons": []},
                         "last_event": None}
            reasons = list(dict.fromkeys(saved["history_status"]["reasons"] + history["reasons"]))
            history = {"status": "incomplete" if reasons else "complete", "reasons": reasons}
            for offset, event in enumerate(incoming):
                if event is None:
                    continue
                # An identical committed page keeps its digest across stop/lift and
                # can be retried after an interrupted receiver checkpoint. Eligibility
                # affects ingestion only, never that page's identity or receipt.
                if not accepts(self.store, scope, datetime.fromisoformat(event.occurred_at).timestamp(), db=db):
                    continue
                require(event.visibility == kind and event.metadata["qq"]["target_id"] == target,
                        "qq_event_scope")
                require(datetime.fromisoformat(event.occurred_at).timestamp()
                        <= self.module.participation.now() + 30, "qq_event_invalid")
                seen_key, body_digest = "qq-seen:" + event.id, digest(event.as_dict())
                previous = self.store.state(seen_key, None, db)
                require(previous in (None, body_digest), "qq_event_conflict")
                self.store.set_state(seen_key, body_digest, db)
                if ReplyAuthorizations(self.store).version(self.module.scope(event), db=db) is None:
                    continue
                if previous is None:
                    require(not events.ingest(self.store, event)["changed"], "qq_event_conflict")
                elif not db.execute("SELECT 1 FROM core_events WHERE id=? AND deleted=0", (event.id,)).fetchone():
                    continue
                if not self.module.merged.retired(event, db):
                    db.execute("INSERT INTO qq_native_inbox(scope,event_id,position) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                               (key, event.id, packet["page"] * 100 + offset))
                saved["last_event"] = event.id
            if saved["last_event"] is not None:
                event = Event.from_dict(events.get(self.store, saved["last_event"])["event"])
                self.module.merged.record_history(event, history)
            self.store.set_state(key, {**saved, "next_page": packet["page"] + 1,
                "complete": packet["complete"], "last_digest": fingerprint, "history_status": history, "catchup":catchup}, db)
        return {"next_page": packet["page"] + 1, "complete": packet["complete"]}

    def pending(self, db):
        result = []
        for key, _ in self._queued_scopes(db):
            saved = self.store.state(key, None, db)
            if saved and saved["complete"]:
                result.extend(row[0] for row in db.execute(
                    "SELECT event_id FROM qq_native_inbox WHERE scope=? ORDER BY position", (key,)))
        return result

    def drop(self, event_id, db):
        # The event ID includes its platform, receiving account and native ID.
        db.execute("DELETE FROM qq_native_inbox WHERE event_id=?", (event_id,))

    def incomplete(self, event, db):
        state = self.state(event.visibility, event.metadata["qq"]["target_id"], db=db)
        return state is not None and not state["complete"]

    def count(self):
        if self.module.intake_options is None:
            return 0
        with self.store.db() as db:
            return sum(count for _,count in self._queued_scopes(db))
