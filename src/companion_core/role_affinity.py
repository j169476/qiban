"""A routing decision based on verified identities and existing relationship scores."""
from .identity import IdentityService
from .persona import observed_tier


def close_contact(store, event):
    if event.platform not in {"bilibili", "wechat", "qq"}:
        return False
    if event.platform != "bilibili" and event.metadata.get(event.platform, {}).get("identity_quality") != "verified":
        return False
    from .contracts import ActorRef
    # IdentityService.person only traverses effective hard links. Suspicions,
    # display names and baseline character settings cannot confer initiative.
    for member in IdentityService(store).person(event.actor)["members"]:
        actor = ActorRef(**member)
        ref = store.state("actor-relationship:" + actor.key)
        identifier = ref["id"] if ref and ref.get("kind") == "relationships" else (
            actor.user_id if actor.platform == "bilibili" else actor.key)
        if store.exists("relationships", identifier) and observed_tier(store.read("relationships", identifier)["data"]) == "亲近":
            return True
    return False
