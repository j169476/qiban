"""Owned native transport for QQModule's ordinary polling and action journal."""
from copy import deepcopy
from threading import Lock

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience, LiveMembers, catchup_since, read, registry
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, require

from .native_process import NativeProcess
from .native_receiver import NativeReceiver
from .native_sender import NativeSender


class NativeProvider:
    def __init__(self, store, config, *, worker_factory=NativeProcess, follow_settings=False):
        self.store, self.config = store, deepcopy(config)
        self.next_target = 0
        self.follow = {}
        self.intake_options = deepcopy(self.config)
        if follow_settings:
            for kind in ("private", "group"):
                category = Audience("qq", self.config["account"]["uin"], kind)
                policy = read(store, category)
                if catchup_since(policy) is not None:
                    self.follow[kind] = (category, policy["version"], LiveMembers())
        worker_config = {**self.config, "follow":dict.fromkeys(self.follow, True)} if self.follow else self.config
        self.worker = worker_factory(worker_config)
        self.verified = self.receiver = self.sender = None
        self.closed = False
        self._open_lock, self._poll_lock = Lock(), Lock()

    def _check_identity(self, identity):
        account = self.config["account"]
        require(isinstance(identity, dict) and identity.get("uin") == account["uin"]
                and identity.get("uid") == account["uid"], "qq_account_mismatch")
        require(isinstance(identity.get("friends"), dict) and isinstance(identity.get("groups"), list),
                "qq_native_catalog_invalid")
        require(all(target in identity["friends"] for target in self.config["private_targets"] if "private" not in self.follow)
                and all(target in identity["groups"] for target in self.config["groups"] if "group" not in self.follow), "qq_target_unverified")
        if self.verified is not None:
            require(all(identity["friends"][target] == self.verified["friends"][target]
                        for target in self.config["private_targets"] if "private" not in self.follow), "qq_native_owner_changed")
        require(not self.closed, "qq_native_disconnected")
        with self.store.lock:
            for kind,(category, revision, members) in self.follow.items():
                if read(self.store,category)["version"] != revision:
                    raise AssistantError("qq_scope_changed","QQ 回复范围已经改变，请重新启动连接")
                group = kind == "group"
                ids = ({i:i for i in identity["groups"]} if group else
                       {k:v for k,v in identity["friends"].items() if k != account["uin"]})
                stamps = identity.get("tenures", {}).get(kind, {})
                require(all(type(stamps.get(i)) is int and stamps[i] > 0 for i in ids), "qq_native_catalog_invalid")
                members.replace(revision, {i:f"{uid}:{stamps[i]}" for i,uid in ids.items()})
                registry(self.store)[category] = members
                self.intake_options["groups" if group else "private_targets"] = list(ids)
        return identity

    def _open(self):
        with self._open_lock:
            require(not self.closed, "qq_native_disconnected")
            if self.verified is None:
                try:
                    self.verified = deepcopy(self._check_identity(self.worker.open()))
                    self.sender = NativeSender(self.store, self.worker, self.verified)
                except BaseException:
                    self.close()
                    raise

    def probe(self):
        self._open()
        self.identity()
        return {"account": self.verified["uin"], "account_label": self.verified["uin"],
                "private_targets": list(self.config["private_targets"]), "groups": list(self.config["groups"])}

    def identity(self):
        self._open()
        try:
            identity = self._check_identity(self.worker.request("identity"))
            self.verified = deepcopy(identity)
            return {"user_id": identity["uin"]}
        except BaseException:
            self.close()
            raise

    def poll_native(self, module, guard):
        with self._poll_lock:
            guard()
            self.identity()
            require(module.store is self.store and module.account == self.verified["uin"], "qq_account_mismatch")
            require(all(module.options[field] == self.config[field] for field in ("private_targets", "groups")),
                    "qq_target_unverified")
            if self.receiver is None:
                self.receiver = NativeReceiver(module, self.worker, self.verified, guard=guard)
            require(self.receiver.module is module, "qq_store_mismatch")
            self.receiver.identity = deepcopy(self.verified)
            self.receiver.guard = guard
            outcomes = []
            targets = [(kind,target) for kind,field in (("private","private_targets"),("group","groups"))
                       for target in self.intake_options[field]]
            if self.follow and targets:
                start = self.next_target % len(targets)
                targets = (targets[start:] + targets[:start])[:8]
                self.next_target = start + len(targets)
            for kind,target in targets:
                guard()
                from companion_core.group_modes import observing
                scope = ReplyScope("qq", module.account, kind, target)
                if ReplyAuthorizations(self.store).version(scope) is not None or observing(self.store, scope):
                    outcomes.append(self.receiver.poll(kind, target))
            guard()
            return outcomes

    def send_action(self, action_id, scope, text):
        require(not self.closed and self.sender is not None, "qq_native_disconnected")
        return self.sender.send_action(action_id, scope, text)

    def send_reply_action(self, action_id, scope, arguments, material, *, guard):
        require(not self.closed and self.sender is not None, 'qq_native_disconnected')
        return self.sender.send_reply_action(action_id,scope,arguments,material,guard=guard)

    def send_bundle_action(self, action_id, scope, arguments, material, *, sticker_revision, guard, file_selection=None):
        from .native_bundle import deliver

        require(not self.closed and self.sender is not None, 'qq_native_disconnected')
        return deliver(self.sender, action_id, scope, arguments, material,
                       sticker_revision=sticker_revision, guard=guard, file_selection=file_selection)

    def save_attachment(self, module, candidate, *, max_bytes, owner, task, guard):
        from .native_media import NativeMedia

        self._open()
        return NativeMedia(self, module).save(candidate, max_bytes=max_bytes, owner=owner, task=task, guard=guard)

    def close(self):
        # No request/poll lock: stopping must interrupt an in-flight native read.
        self.closed = True
        self.worker.close()  # Interrupt a native submit before waiting for its ledger lock.
        if not self.follow:
            return
        with self.store.lock:
            for category, _, members in self.follow.values():
                members.disconnect()
                if registry(self.store).get(category) is members:
                    registry(self.store).pop(category)

    interrupt = close
