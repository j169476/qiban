from dataclasses import replace

import pytest

from companion_core import events
from companion_core.access import RecordPolicy, RequestAuthority
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.relations import RelationService


def test_bounded_graph_hides_other_private_claim_and_invalidated_evidence(pg_store):
    repo = GovernanceRepository(pg_store)
    authority = RequestAuthority(epoch=repo.epoch)
    service = RelationService(repo, authority)
    actors = [ActorRef("alpha", str(i)) for i in range(4)]
    for actor in actors:
        service.register_actor(actor)
    requests = [authority.issue(actor=actor, conversation="room-" + actor.key, audience="private", recipients=(actor,))
                for actor in actors]
    sources = []
    for index, actor in enumerate(actors):
        event = Event("alpha", "bot", str(index), actor, "room-" + actor.key, "2026-10-23T00:00:00Z", "关系声明", "private")
        events.ingest(pg_store, event)
        sources.append(event)
    first = service.propose(requests[0], subject=actors[0].key, object=actors[1].key,
                            relation="同事", evidence=["event/" + sources[0].id])
    second = service.propose(requests[0], subject=actors[1].key, object=actors[2].key,
                             relation="同学", evidence=["event/" + sources[0].id])
    service.propose(requests[1], subject=actors[1].key, object=actors[3].key,
                    relation="private-hidden-edge", evidence=["event/" + sources[1].id])
    direct = service.query(requests[0], actors[0].key)
    assert [item["id"] for item in direct["claims"]] == [first["id"]]
    wider = service.query(requests[0], actors[0].key, depth=2)
    assert {item["id"] for item in wider["claims"]} == {first["id"], second["id"]}
    assert "private-hidden-edge" not in str(wider) and not wider["inferred_relationships"]
    with pytest.raises(AssistantError):
        service.query(requests[0], actors[0].key, depth=3)
    assert all(item["status"] == "claimed" for item in wider["claims"])
    events.ingest(pg_store, replace(sources[0], text="撤回原先的关系描述"))
    assert not service.query(requests[0], actors[0].key, depth=2)["claims"]


def test_hidden_endpoints_do_not_consume_visible_graph_limit(pg_store):
    repo = GovernanceRepository(pg_store)
    authority = RequestAuthority(epoch=repo.epoch)
    service = RelationService(repo, authority)
    actors = [ActorRef("alpha", str(i)) for i in range(5)]
    for actor in actors:
        service.register_actor(actor)
    request = authority.issue(actor=actors[0], conversation="local", audience="private", recipients=(actors[0],))
    for index, actor in enumerate(actors[1:]):
        identifier = f"edge-{index}"
        if index < 3:
            old = repo.policy("actor/" + actor.key)
            repo.set_policy(replace(old, classification="secret", version="hidden"), old.version, "fixture")
        with pg_store.db() as db:
            db.execute("INSERT INTO relationship_claims VALUES(?,?,?,?,?,?,?)",
                       (identifier, actors[0].key, actor.key, actors[0].key, "claimed", "v1",
                        pg_store.ledger_encode("relationship_claims.body", {
                            "relation": "colleague", "reason": "synthetic", "evidence": [], "direction": "subject_to_object"})))
        repo.set_policy(RecordPolicy("relation/" + identifier, "v1", "public"), None, "fixture")
    result = service.query(request, actors[0].key, max_edges=1)
    assert [item["id"] for item in result["claims"]] == ["edge-3"]
    assert not result["truncated"]
