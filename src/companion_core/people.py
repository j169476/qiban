"""Source-preserving person views over linked identities and editable impressions."""
from .contracts import ActorRef
from .errors import AssistantError
from .identity import IdentityService


def bind_relationship(store, actor, record_id):
    store.set_state("actor-relationship:" + actor.key, {"kind": "relationships", "id": record_id})


def profile(store, actor):
    return IdentityService(store).decision_context(actor)["person_profile"]


def profile_snapshot(store, person):
    """Read each impression once, preserving its mapping and canonical-file dependency."""
    impressions = []
    dependencies = []
    with store.db() as db:
        references = {ActorRef(**member).key: store.state("actor-relationship:" + ActorRef(**member).key, db=db)
                      for member in person["members"]}
    for member in person["members"]:
        key = ActorRef(**member).key
        reference = references[key]
        dependency = {"actor": key, "reference": reference}
        dependencies.append(dependency)
        if not reference:
            continue
        if (not isinstance(reference, dict) or reference.get("kind") != "relationships"
                or not isinstance(reference.get("id"), str)):
            dependency["error"] = "invalid_relationship_reference"
            continue
        try:
            record = store.read(reference["kind"], reference["id"])
        except AssistantError as exc:
            dependency["error"] = exc.code
            try:
                dependency["version"] = store.version(reference["kind"], reference["id"])
            except (AssistantError, OSError):
                dependency["version"] = None
            continue
        dependency["version"] = record["version"]
        data = record["data"]
        impressions.append({"actor": member, "reference": {**reference, "version": record["version"]},
                            "display_name": data["display_name"], "affinity": data["affinity"],
                            "impression": data.get("impression", ""), "facts": data.get("facts", [])})
    result = {**person, "impressions": impressions, "affinity_aggregation": "preserve_per_identity"}
    # The view is derived from YAML; reading it must not rewrite database state.
    return result, dependencies
