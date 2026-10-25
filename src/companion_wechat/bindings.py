"""Explicit verified bindings in the existing store; display names are not identities."""
from dataclasses import asdict, dataclass, field, replace

from companion_core.contracts import stable_key

from .contracts import identifier, require


@dataclass(frozen=True)
class ConversationBinding:
    key: str
    target_ref: str
    kind: str
    peer_ref: str = ""
    members: dict = field(default_factory=dict)
    revision: int = 1
    enabled: bool = True

    def __post_init__(self):
        require(identifier(self.key) and identifier(self.target_ref))
        require(self.kind in ("private", "group"))
        require(self.kind != "private" or identifier(self.peer_ref))
        require(isinstance(self.members, dict)
                and all(identifier(k) and identifier(v) for k, v in self.members.items()))
        require(type(self.revision) is int and self.revision >= 1 and type(self.enabled) is bool)


class BindingRegistry:
    """Only trusted setup code may call bind; P0 title matches never call it.

    Account identity and target_ref must be independently verified opaque IDs.
    The registry does not turn a UI title or a caller assertion into verification.
    No configuration or live adapter is wired into the production service in P1.
    """

    def __init__(self, store, account, account_identity):
        require(identifier(account) and identifier(account_identity))
        self.store, self.account, self.identity = store, account, account_identity
        self.key = stable_key("wechat-bindings-v1", account)
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            data = store.state(self.key, None, db)
            if data is None:
                store.set_state(self.key, {"identity": account_identity, "conversations": {}}, db)
            else:
                require(data["identity"] == account_identity, "wechat_account_rebind")

    def bind(self, binding, *, expected_revision=None):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            data = self.store.state(self.key, None, db)
            old = data["conversations"].get(binding.key)
            require((old["revision"] if old else None) == expected_revision, "wechat_binding_conflict")
            # Reassignment of an existing conversation to another person/group
            # would give the new target the old conversation's history.
            if old:
                require(all(old[k] == getattr(binding, k) for k in ("target_ref", "kind", "peer_ref")),
                        "wechat_target_reassignment")
            require(not any(b["target_ref"] == binding.target_ref and k != binding.key
                            for k, b in data["conversations"].items()), "wechat_duplicate_target")
            saved = replace(binding, revision=(old["revision"] + 1 if old else 1))
            data["conversations"][binding.key] = asdict(saved)
            self.store.set_state(self.key, data, db)
            return saved

    def get(self, key, *, db=None):
        data = self.store.state(self.key, None, db)
        value = data["conversations"].get(key)
        require(value is not None, "wechat_target_unbound")
        return ConversationBinding(**value)

    def for_target(self, target_ref, *, db=None):
        data = self.store.state(self.key, None, db)
        values = [v for v in data["conversations"].values() if v["target_ref"] == target_ref]
        require(len(values) == 1, "wechat_target_unbound")
        return ConversationBinding(**values[0])
