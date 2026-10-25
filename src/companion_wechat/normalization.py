"""Immutable inbound reservations share the core's storage and encryption boundary."""
from companion_core.contracts import ActorRef, Event, stable_key

from .contracts import digest, require


class Normalizer:
    def __init__(self, bindings, *, include_untriggered_group=False, verified_sender_ids=False):
        self.bindings, self.store = bindings, bindings.store
        self.include_untriggered_group = include_untriggered_group
        self.verified_sender_ids = verified_sender_ids

    def batch(self, observations, connection):
        result = []
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in observations:
                require(item.account_identity == self.bindings.identity, "wechat_account_mismatch")
                key = stable_key("wechat-observation-v1", self.bindings.account, item.observation_id)
                prior = self.store.state(key, None, db)
                if prior:
                    require(prior["fingerprint"] == item.fingerprint, "wechat_observation_conflict")
                    event = prior["event"]
                else:
                    require(item.epoch == connection.epoch, "wechat_stale_connection")
                    event = self._normalize(item, db)
                    event = event.as_dict() if event else None
                    self.store.set_state(key, {"fingerprint": item.fingerprint, "event": event}, db)
                if event:
                    parsed = Event.from_dict(event)
                    # Also inspect tombstones. A bridge must never turn an ID
                    # collision into a source edit or resurrect deleted material.
                    row = db.execute("SELECT body,deleted FROM core_events WHERE id=?", (parsed.id,)).fetchone()
                    if row:
                        require(digest(self.store.ledger_decode("core_events.body", row["body"])) == digest(event),
                                "wechat_event_conflict")
                        if row["deleted"]:
                            continue
                    result.append(parsed)
        # Same exact observation twice in a batch is one event; two equal texts
        # with distinct observation IDs remain two events.
        return list({event.id: event for event in result}.values())

    def _normalize(self, item, db):
        binding = self.bindings.for_target(item.target_ref, db=db)
        require(binding.kind == item.kind, "wechat_target_kind_mismatch")
        require(binding.enabled, "wechat_target_disabled")
        if item.direction != "incoming" or not item.text.strip():
            return None
        if (binding.kind == "group" and not self.include_untriggered_group
                and item.trigger not in ("mention", "reply")):
            return None
        account = self.bindings.account
        if binding.kind == "private":
            require(item.sender_ref == binding.peer_ref, "wechat_sender_mismatch")
            actor = ActorRef("wechat", binding.peer_ref, account)
            quality = "verified"
        elif item.sender_ref in binding.members or self.verified_sender_ids:
            actor = ActorRef("wechat", binding.members.get(item.sender_ref, item.sender_ref), account)
            quality = "verified"
        else:
            # A nickname cannot safely distinguish two members of the same group.
            # Provisional actors are observation-local, never stable name hashes.
            actor = ActorRef("wechat", item.observation_id, stable_key("wechat-provisional", account, binding.key))
            quality = "observation_only"
        return Event("wechat", account, item.observation_id, actor, binding.key, item.occurred_at,
                     item.text, binding.kind, metadata={
                         "wechat": {"binding_revision": binding.revision, "target_ref": binding.target_ref,
                                    "epoch": item.epoch, "identity_quality": quality,
                                    "trigger": item.trigger, "time_basis": item.time_basis,
                                    "observation_fingerprint": item.fingerprint}})
