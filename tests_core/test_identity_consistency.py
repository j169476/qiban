"""Identity decisions must remain attributable to live, editable source records."""
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from time import perf_counter

import pytest
import yaml
from test_platform_core import event, proposal, reference, suspected

from companion_core import events
from companion_core.contracts import ActorRef, stable_key
from companion_core.errors import AssistantError
from companion_core.identity import IdentityService
from companion_core.people import bind_relationship
from companion_core.storage import Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "core")
    assert result.initialize()["ok"]
    return result


def relationship(store, actor, *, record_id=None, score=52):
    item_id = record_id or actor.key
    store.write("relationships", {"schema_version": 1, "id": item_id, "display_name": actor.platform,
                "affinity": score, "interactions": 1, "events": [], "locked": False,
                "impression": "一起讨论过蓝色桌面的草图", "facts": ["偏爱圆角"]}, None, "test", item_id)
    bind_relationship(store, actor, item_id)


def manual_link(store, left, right, *, status="hard"):
    item_id = stable_key("link", *sorted((left.key, right.key)))
    data = {"schema_version": 1, "id": item_id, "left": left.as_dict(), "right": right.as_dict(),
            "status": status, "reason": "人工确认的合成关联，仅供本地测试", "evidence": [],
            "history": [], "manual": True, "locked": True}
    store.path("identity_links", item_id).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return data


@pytest.mark.parametrize("change", ["revoke", "delete_link", "edit_link", "new_candidate", "extend_person",
                                   "edit_evidence", "delete_evidence", "edit_impression", "delete_impression",
                                   "change_mapping", "remove_mapping", "invalid_impression"])
def test_decision_dependency_rejects_changes_to_shared_identity_inputs(store, change):
    service, first, second, _, a = suspected(store)
    link = service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    relationship(store, first.actor, score=60)
    relationship(store, second.actor)
    frozen = service.decision_context(first.actor)
    assert sorted(i["affinity"] for i in frozen["person_profile"]["impressions"]) == [52, 60]
    service.verify_context(json.loads(json.dumps(frozen["dependency"])))

    if change == "revoke":
        service.revoke(link["data"]["id"], link["version"], "人工确认两账号并非同一人")
    elif change == "delete_link":
        store.path("identity_links", link["data"]["id"]).unlink()
    elif change == "edit_link":
        data = deepcopy(link["data"])
        data["reason"] = "本地修改了证据解释，旧模型不得继续依赖旧解释"
        store.write("identity_links", data, link["version"], "user", data["id"])
    elif change == "new_candidate":
        manual_link(store, first.actor, ActorRef("gamma", "new"), status="suspected")
    elif change == "extend_person":
        manual_link(store, second.actor, ActorRef("gamma", "new"))
    elif change == "edit_evidence":
        events.ingest(store, replace(second, text="我之前说错了，并不是那个人。"))
    elif change == "delete_evidence":
        events.forget(store, second.id)
    elif change == "edit_impression":
        path = store.path("relationships", second.actor.key)
        previous = path.stat()
        path.write_bytes(path.read_bytes().replace(b"affinity: 52", b"affinity: 55"))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert path.stat().st_size == previous.st_size
    elif change == "delete_impression":
        store.path("relationships", second.actor.key).unlink()
    elif change == "change_mapping":
        relationship(store, second.actor, record_id="alternate-impression", score=12)
    elif change == "remove_mapping":
        store.set_state("actor-relationship:" + second.actor.key, None)
    elif change == "invalid_impression":
        store.path("relationships", second.actor.key).write_text("not: [valid", encoding="utf-8")

    with pytest.raises(AssistantError) as rejected:
        service.verify_context(frozen["dependency"])
    assert rejected.value.code == "context_changed"


def test_unrelated_identity_and_impression_changes_do_not_invalidate_decision(store):
    service, first, second, _, a = suspected(store)
    service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    frozen = service.decision_context(first.actor)["dependency"]
    stranger = ActorRef("alpha", "unrelated")
    manual_link(store, stranger, ActorRef("beta", "another-person"))
    relationship(store, stranger)
    events.ingest(store, event(uid="unrelated", native="unrelated", text="与当前人物无关的合成消息"))
    service.verify_context(frozen)


@pytest.mark.parametrize("same_actor", [True, False])
def test_invalid_local_identity_edge_is_reported_and_isolated(store, same_actor):
    left = ActorRef("alpha", "100")
    right = left if same_actor else ActorRef("alpha", "101")
    data = manual_link(store, left, right, status="revoked")
    path = store.path("identity_links", data["id"])
    with pytest.raises(AssistantError) as rejected:
        store.profile.validate("identity_links", data)
    assert rejected.value.code == "invalid_data"
    validation = store.validate_all()
    assert not validation["ok"]
    assert any(issue["file"] == str(path) for issue in validation["issues"])
    service = IdentityService(store)
    context = service.decision_context(ActorRef("beta", "unrelated"))
    assert not context["identity"]["person"]["inferred"]
    assert context["identity"]["issues"]
    service.verify_context(context["dependency"])
    assert path.exists()  # Local edits are diagnosed, never silently overwritten.
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_identity_links").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_old_self_claim_remains_available_for_later_cross_platform_correspondence(store):
    service, first, second, _, anchor = suspected(store)
    for i in range(6):
        events.ingest(store, replace(first, native_id=f"chatter-{i}", text=f"今天的普通闲聊第{i}条，没有身份信息。",
                                     occurred_at=f"2026-10-22T05:00:0{i}+00:00"))
    other = reference(store, replace(second, occurred_at="2026-10-22T06:00:00+00:00"))
    assert first.id not in {r["event"]["id"] for r in events.recent(store, actor=first.actor, limit=4)}

    async def generate(instructions, context, schema):
        provided = {r["event"]["id"] for r in context["identity_evidence"]}
        assert {first.id, second.id} <= provided
        assert len(context["identity_evidence"]) <= 9
        assert "history" not in context["candidates"][0]["data"]
        return {"identity_proposals": [proposal(first.actor, second.actor, [anchor, other], "correspondence", True)]}

    accepted = await service.review(second.actor, generate, lambda: None)
    assert accepted[0]["data"]["status"] == "hard"
    ordinary = service.decision_context(second.actor)
    serialized = json.dumps(ordinary, ensure_ascii=False)
    assert first.text not in serialized
    assert "quote" not in serialized


@pytest.mark.asyncio
async def test_identity_review_cannot_undo_a_concurrent_manual_revocation(store):
    service, first, second, old, a = suspected(store)
    b = reference(store, second)

    async def generate(instructions, context, schema):
        service.revoke(old["data"]["id"], old["version"], "评估过程中人工撤销关联")
        return {"identity_proposals": [proposal(first.actor, second.actor, [a, b], "correspondence", True)]}

    with pytest.raises(AssistantError) as rejected:
        await service.review(second.actor, generate, lambda: None)
    assert rejected.value.code == "context_changed"
    assert service.links()[0]["effective_status"] == "revoked"


def test_bounded_link_history_retains_original_and_independent_evidence(store):
    service, first, second, _, a = suspected(store)
    service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    for i in range(32):
        followup = replace(first, native_id=f"additional-claim-{i}", text=first.text + f"这是第{i}次补充说明。")
        service.apply(proposal(first.actor, second.actor, [reference(store, followup)]))
    link = service.links()[0]
    kept = {r["event_id"] for r in link["data"]["evidence"]}
    assert len(kept) == 30
    assert {first.id, second.id} <= kept
    assert link["effective_status"] == "hard"


def test_identity_review_excerpt_budget_keeps_exact_anchors_for_many_long_messages(store):
    service = IdentityService(store)
    expected = []
    actors = {}
    for i in range(5):
        number = str(7000000 + i)
        first_quote = f"我的另一平台账号是 {number}，昨天讨论过蓝色桌面草图。" + "甲" * 1900
        second_quote = "我是昨天讨论蓝色桌面草图的那个人，当时约好采用圆角。" + "乙" * 1900
        first = event(native=f"anchor-left-{i}", text="首" * 15000 + first_quote + "尾" * 15000)
        second = event("beta", number, native=f"anchor-right-{i}", text="首" * 15000 + second_quote + "尾" * 15000)
        first = replace(first, metadata={"never-export-this-metadata": "x" * 20000})
        a, b = reference(store, first, first_quote), reference(store, second, second_quote)
        expected.extend((a, b))
        service.apply(proposal(first.actor, second.actor, [a]))
        service.apply(proposal(first.actor, second.actor, [a, b], "correspondence", True))
        actors[first.actor.key], actors[second.actor.key] = first, second
    for original in actors.values():
        for i in range(4):
            events.ingest(store, replace(original, native_id=f"recent-{original.actor.user_id}-{i}", text="普通长消息" * 6000,
                                         occurred_at=f"2026-10-22T05:00:0{i}+00:00"))
    context = service.context(first.actor, include_evidence=True)
    serialized = json.dumps(context, ensure_ascii=False)
    assert context["evidence_text_characters"] <= service.REVIEW_TEXT_LIMIT
    assert len(serialized) < 48000
    assert context["evidence_omitted"] > 0
    assert "never-export-this-metadata" not in serialized
    for ref in expected:
        assert any(item["event"]["id"] == ref["event_id"] and item["version"] == ref["version"]
                   and ref["quote"] in item["event"]["text"] for item in context["evidence"])
        service._evidence([ref])  # Every visible anchor still validates against its exact full source.


@pytest.mark.asyncio
async def test_identity_review_rejects_changes_to_recent_messages_even_when_not_cited(store):
    service, first, second, _, a = suspected(store)
    b = reference(store, second)
    recent = replace(first, native_id="uncited-recent", text="这条消息会参与身份判断但没有被最终提议直接引用。")
    events.ingest(store, recent)

    async def generate(instructions, context, schema):
        assert recent.id in {item["event"]["id"] for item in context["identity_evidence"]}
        events.forget(store, recent.id)
        return {"identity_proposals": [proposal(first.actor, second.actor, [a, b], "correspondence", True)]}

    with pytest.raises(AssistantError) as rejected:
        await service.review(second.actor, generate, lambda: None)
    assert rejected.value.code == "context_changed"
    assert service.links()[0]["effective_status"] == "suspected"


def measured_database(store):
    real_db = store.db
    counts = {"opens": 0, "writes": 0, "proof_queries": 0}

    def trace(sql):
        upper = sql.upper()
        if "CORE_IDENTITY_LINKS" in upper and upper.startswith(("INSERT", "UPDATE", "DELETE")):
            counts["writes"] += 1
        if "SELECT ID,BODY,VERSION,DELETED FROM CORE_EVENTS" in upper:
            counts["proof_queries"] += 1

    @contextmanager
    def measured():
        counts["opens"] += 1
        with real_db() as db:
            db.set_trace_callback(trace)
            yield db

    store.db = measured
    return counts


def test_thousand_edge_context_reads_have_constant_connections_and_no_rewrites(store):
    for i in range(1000):
        manual_link(store, ActorRef("alpha", str(i)), ActorRef("beta", str(i)))
    service = IdentityService(store)
    actor = ActorRef("alpha", "500")
    counts = measured_database(store)
    started = perf_counter()
    service.decision_context(actor)
    initial = {"seconds": round(perf_counter() - started, 4), **counts}
    assert initial["opens"] <= 3
    assert initial["writes"] == 1000
    counts.update(opens=0, writes=0, proof_queries=0)
    started = perf_counter()
    frozen = service.decision_context(actor)
    elapsed = perf_counter() - started
    assert frozen["identity"]["person"]["inferred"]
    assert counts["opens"] <= 2
    assert counts["writes"] == 0
    print(json.dumps({"graph_edges": 1000, "initial": initial,
                      "unchanged": {"seconds": round(elapsed, 4), **counts}}))

    data = manual_link(store, ActorRef("alpha", "500"), ActorRef("beta", "500"), status="revoked")
    before = counts.copy()
    with pytest.raises(AssistantError):
        service.verify_context(frozen["dependency"])
    assert counts["writes"] - before["writes"] == 1
    store.path("identity_links", data["id"]).unlink()
    before = counts.copy()
    service.decision_context(actor)
    assert counts["writes"] - before["writes"] == 1


def test_many_evidence_references_use_batched_reads_and_changed_edge_updates(store):
    for i in range(30):
        first = event(uid=str(i), native=f"evidence-{i}")
        proof = reference(store, first)
        data = manual_link(store, first.actor, ActorRef("beta", str(i)), status="suspected")
        data.update(manual=False, locked=False, evidence=[proof])
        store.path("identity_links", data["id"]).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    service = IdentityService(store)
    service.decision_context(first.actor)
    counts = measured_database(store)
    service.decision_context(first.actor)
    assert counts == {"opens": 2, "writes": 0, "proof_queries": 1}
    events.forget(store, first.id)
    before = counts.copy()
    assert not service.context(first.actor)["candidates"]
    assert counts["writes"] - before["writes"] == 1
