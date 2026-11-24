"""Durable frozen-range intake. Native reads finish before pages reach the core.

Staging may restart from its saved upper bound before publication. Once ready,
only encrypted saved pages are used, including after a native process restart.
"""
from copy import deepcopy

from companion_core.contracts import stable_key
from companion_core.history_status import validate_history_status
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, require
from companion_core.reply_exclusion import ReplyExclusions


class Suspended(Exception):
    """The administrator stopped this one conversation while it was being read.

    Never an AssistantError: it must not look like an account-level fault to the
    provider, which keeps reading the other conversations.
    """


class NativeReceiver:
    def __init__(self, module, worker, identity, *, guard, page_size=100):
        require(type(page_size) is int and 1 <= page_size <= 100, "qq_native_page_invalid")
        self.module, self.store, self.worker = module, module.store, worker
        self.identity, self.guard, self.page_size = deepcopy(identity), guard, page_size

    def key(self, kind, target):
        return self.module.native_inbox.key(kind, target) + ":receiver"

    def _scope(self, kind, target):
        require(self.identity["uin"] == self.module.account, "qq_account_mismatch")
        require(target in (self.identity["groups"] if kind == "group" else self.identity["friends"]),
                "qq_target_unverified")
        return ReplyScope("qq", self.module.account, kind, target)

    def _excluded(self, scope, db=None):
        return ReplyExclusions(self.store).record(scope, db=db)["excluded"]

    def _owner(self, kind, target, db=None):
        """Account, target and durable authorization identity, never the exclusion permit.

        The saved owner must survive a single-conversation stop and its lift (that is
        not an account or relationship change), while a real account, target or
        authorization change still invalidates the frozen range.
        """
        scope = self._scope(kind, target)
        from companion_core.reply_audience import Audience, read
        policy = read(self.store, Audience("qq", self.module.account, kind), db)
        if policy["enabled"]:
            authorization = policy["version"]
        else:
            record = ReplyAuthorizations(self.store).record(scope, db=db)
            authorization = (record["version"] if record["enabled"]
                             and record.get("member_present", True) else None)
            # Keep the pre-exclusion owner convention after allow-all was disabled.
            # Only the exclusion wrapper is absent here, not the category revision.
            if authorization and policy["version"]:
                authorization = stable_key("manual-reply", policy["version"], authorization)
            if not authorization and kind == "group":
                from companion_core.group_modes import settings
                mode = settings(self.store, scope, db)
                if mode["listen"]:
                    authorization = stable_key("observe", scope.id, mode["since"])
        return {"uin": self.identity["uin"], "uid": self.identity["uid"], "authorization": authorization,
                "peer": target if kind == "group" else self.identity["friends"][target]}

    def _head(self, kind, target, *, allow_empty=False):
        self.guard()
        value = self.worker.request(kind + "_head", target=target, **({"allow_empty": True} if allow_empty else {}))
        self.guard()
        if value is None and allow_empty:
            return None
        return self.module.native_inbox._bound(value, kind, target, self.identity)

    def enroll(self, kind, target, checkpoint):
        """Trusted initial registration only; never rewind an existing receiver."""
        key = self.key(kind, target)
        value = {"phase": "idle", "lower": self.module.native_inbox._bound(checkpoint, kind, target, self.identity),
                 "owner": self._owner(kind, target), "page_size": self.page_size, "staged": 0}
        self._save(kind, target, key, None, value)

    def _save(self, kind, target, key, previous, value, *, packet=None, clear=False):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard()
            # A stop written while this range was being read suspends only this scope:
            # nothing is committed, so the position and its dedupe stay exactly as they
            # were and the account keeps reading the other conversations.
            if self._excluded(self._scope(kind, target), db):
                raise Suspended()
            require(self._owner(kind, target, db) == value["owner"], "qq_native_owner_changed")
            require(self.store.state(key, None, db) == previous, "qq_native_receiver_changed")
            if clear and previous:
                for index in range(previous["staged"]):
                    db.execute("DELETE FROM state WHERE key=?", (key + ":page:" + str(index),))
            if packet is not None:
                self.store.set_state(key + ":page:" + str(packet["page"]), packet, db)
            self.store.set_state(key, value, db)

    def poll(self, kind, target):
        try:
            return self._poll(kind, target)
        except Suspended:
            return {"status": "excluded"}

    def _poll(self, kind, target):
        self.guard()
        key = self.key(kind, target)
        with self.store.lock:
            owner = self._owner(kind, target)
            scope = self._scope(kind, target)
            if self._excluded(scope):
                # Stop reading this one conversation, without closing the provider.
                return {"status": "excluded"}
            from companion_core.group_modes import observing
            require(ReplyAuthorizations(self.store).version(scope) is not None or observing(self.store, scope),
                    "reply_not_authorized")
        saved = self.store.state(key)
        if saved is None:
            from companion_core.reply_audience import Audience, catchup_since, read
            since = catchup_since(read(self.store,Audience("qq",self.module.account,kind)))
            if since is not None:
                upper = self._head(kind,target,allow_empty=True)
                if upper is None:
                    return {"status":"waiting_for_first_message"}
                fresh = {"phase":"collecting","lower":None,"upper":upper,"since":since,
                         "owner":owner,"page_size":self.page_size,"staged":0}
                self._save(kind,target,key,None,fresh)
                return self._collect(kind,target,key,fresh)
            # Previously unseen conversations start at registration, not old history.
            self.enroll(kind, target, self._head(kind, target))
            return {"status": "registered"}
        require(saved["owner"] == owner and saved["page_size"] == self.page_size, "qq_native_owner_changed")
        if saved["phase"] == "ready":
            return self._publish(kind, target, key, saved)
        if saved["phase"] == "idle":
            with self.store.db() as db:
                queued = db.execute("SELECT 1 FROM qq_native_inbox WHERE scope=? LIMIT 1",
                    (self.module.native_inbox.key(kind, target),)).fetchone()
            if queued:
                return {"status": "waiting_for_batch"}
            upper = self._head(kind, target)
            if upper == saved["lower"]:
                return {"status": "idle"}
            pending = {**saved, "phase": "collecting", "upper": upper}
            self._save(kind, target, key, saved, pending)
            saved = pending
        require(saved["phase"] == "collecting", "qq_native_receiver_changed")
        return self._collect(kind, target, key, saved)

    def _collect(self, kind, target, key, saved):
        # No staged page is visible to scheduling. Partial staging can be rebuilt
        # from the same endpoints without changing any published page boundary.
        reset = {**saved, "staged": 0}
        self._save(kind, target, key, saved, reset, clear=True)
        saved = reset
        args = {"group" if kind == "group" else "target": target,
                "lower": saved["lower"], "upper": saved["upper"]}
        if "since" in saved:
            args["since"] = saved["since"]
        window = self.worker.request(kind + "_open", **args)
        self.guard()
        require(isinstance(window, dict) and set(window) == {"window_id", "upper", "count", "history_status"}
                and window["upper"] == saved["upper"] and type(window["count"]) is int
                and 0 <= window["count"] <= 10000 and isinstance(window["window_id"], str)
                and 1 <= len(window["window_id"]) <= 128, "qq_native_page_invalid")
        history = validate_history_status(window["history_status"])
        require(history is not None, "qq_native_page_invalid")
        pages = max(1, (window["count"] + self.page_size - 1) // self.page_size)
        identifier = stable_key("qq-frozen", window["window_id"], self.page_size)
        buffer, received = [], 0
        for source_page in range(max(1, (window["count"] + 99) // 100)):
            self.guard()
            packet = self.worker.request(kind + "_page", window_id=window["window_id"], page=source_page)
            expected = {"window_id": window["window_id"], "kind": kind, "target": target,
                        "lower": saved["lower"], "upper": saved["upper"], "page": source_page,
                        "complete": (source_page + 1) * 100 >= window["count"], "history_status": history}
            require(isinstance(packet, dict) and set(packet) == set(expected) | {"messages"}
                    and all(packet[k] == v for k, v in expected.items())
                    and isinstance(packet["messages"], list)
                    and len(packet["messages"]) == min(100, window["count"] - received), "qq_native_page_invalid")
            if "since" in saved:
                require(all(isinstance(raw,dict) and str(raw.get("msgTime","")).isdecimal()
                            and int(raw["msgTime"]) >= saved["since"] for raw in packet["messages"]),
                        "qq_native_message_outside_window")
            buffer.extend(packet["messages"])
            received += len(packet["messages"])
            while len(buffer) >= self.page_size or packet["complete"]:
                part, buffer = buffer[:self.page_size], buffer[self.page_size:]
                index = saved["staged"]
                staged = {**expected, "window_id": identifier, "page": index, "complete": index + 1 == pages,
                          "messages": part}
                updated = {**saved, "staged": index + 1}
                self._save(kind, target, key, saved, updated, packet=staged)
                saved = updated
                if not buffer:
                    break
        require(saved["staged"] == pages and received == window["count"] and not buffer, "qq_native_page_invalid")
        ready = {**saved, "phase": "ready", "next_page": 0}
        self._save(kind, target, key, saved, ready)
        return {"status": "staged", "pages": pages, "messages": received}

    def _publish(self, kind, target, key, saved):
        with self.store.lock:
            self.guard()
            scope = self._scope(kind, target)
            if self._excluded(scope):
                # Keep the staged pages and the position: a later read after the lift
                # re-checks every message against the exclusion start.
                raise Suspended()
            require(self._owner(kind, target) == saved["owner"], "qq_native_owner_changed")
            packet = self.store.state(key + ":page:" + str(saved["next_page"]))
            require(packet is not None and packet["page"] == saved["next_page"], "qq_native_page_invalid")
            receipt = self.module.accept_native_page(packet, identity=self.identity,
                                                    **({"catchup": True} if "since" in saved else {}))
            require(receipt == {"next_page": saved["next_page"] + 1, "complete": packet["complete"]},
                    "qq_native_page_invalid")
            updated = {**saved, "next_page": receipt["next_page"]}
            if packet["complete"]:
                updated = {"phase": "idle", "lower": saved["upper"], "owner": saved["owner"],
                           "page_size": self.page_size, "staged": 0}
            self._save(kind, target, key, saved, updated, clear=packet["complete"])
        return {"status": "published", **receipt}
