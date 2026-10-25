from datetime import timedelta

import pytest
from test_learning import SearchAdapter, goal, model_stub, request

from bili_assistant.errors import AssistantError
from bili_assistant.learning import create, step
from bili_assistant.learning_tree import configure, nodes_for, status
from bili_assistant.research import attach, capture, save_note
from bili_assistant.scheduler import Scheduler


async def complete(role, monkeypatch, fail=False):
    saved = goal(role)
    role.adapter = SearchAdapter()
    calls = []
    for _ in range(6):
        scheduler = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(scheduler, "model", model_stub(role, calls, fail))
        await step(scheduler)
    return saved["data"]["id"], calls


@pytest.mark.asyncio
async def test_mastery_reviews_restart_and_repeated_finalization_are_idempotent(role, monkeypatch):
    from bili_assistant.learning_tree import apply_assessment

    goal_id, calls = await complete(role, monkeypatch)
    tree = status(role, goal_id)
    node = tree["nodes"][0]
    assert node["evidence_current"] and node["data"]["mastery"] == 1 and node["data"]["streak"] == 1
    assert not node["due"]
    check = role.store.read("learning_checks", node["data"]["assessment"]["id"])
    apply_assessment(role, tree["goal"], check, check)
    assert role.store.read("learning_nodes", node["data"]["id"])["version"] == node["version"]
    now = role.clock() + timedelta(days=4)
    role.clock = lambda: now
    for expected in ("answered", "graded"):
        scheduler = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
        result = await step(scheduler)
        assert result["type"] == "review" and result["phase"] == expected
    current = status(role, goal_id)["nodes"][0]
    assert current["data"]["streak"] == 2 and current["evidence_current"] and not current["due"]
    assert len(calls) == 8
    assert await step(Scheduler(role, role.run()["generation"])) is None
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_failed_review_records_mistakes_and_shortens_interval(role, monkeypatch):
    goal_id, calls = await complete(role, monkeypatch)
    now = role.clock() + timedelta(days=4)
    role.clock = lambda: now
    for _ in range(2):
        scheduler = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(scheduler, "model", model_stub(role, calls, fail=True))
        await step(scheduler)
    node = status(role, goal_id)["nodes"][0]["data"]
    assert node["mastery"] == 0 and node["streak"] == 0
    assert node["next_review_at"] == (now + timedelta(days=1)).isoformat()
    assert len(node["mistakes"]) == 2 and all(not m["resolved"] for m in node["mistakes"])
    assert all(m["assessment"]["kind"] == "learning_reviews" for m in node["mistakes"])


@pytest.mark.asyncio
async def test_local_material_edit_invalidates_mastery_and_stops_due_review(role, monkeypatch):
    goal_id, _ = await complete(role, monkeypatch)
    now = role.clock() + timedelta(days=4)
    role.clock = lambda: now
    note = role.store.records("knowledge")[0][0]
    note["data"]["content"] += " 本地修正"
    role.store.write("knowledge", note["data"], note["version"], "user", note["data"]["id"])
    assert not status(role, goal_id)["nodes"][0]["evidence_current"]
    scheduler = Scheduler(role, role.run()["generation"])
    assert await step(scheduler) is None
    role.stop(paused=True)
    with pytest.raises(AssistantError):
        await step(scheduler)


def test_tree_links_reject_cross_goal_and_cycles_without_changing_scores(role):
    turn, quote = request(role)
    saved = create(role, "调试方法", ["理解变量", "说明适用边界"], turn, quote)
    a, b = nodes_for(role.store, saved)
    changed = configure(role, b["data"]["id"], b["version"], a["data"]["id"], [])
    assert changed["data"]["mastery"] is None
    with pytest.raises(AssistantError, match="成环"):
        configure(role, a["data"]["id"], a["version"], None, [b["data"]["id"]])
    with pytest.raises(AssistantError, match="缺失"):
        configure(role, a["data"]["id"], a["version"], "other-goal-node", [])


@pytest.mark.asyncio
async def test_native_web_material_enters_existing_goal_quiz_without_bili_read(role, monkeypatch):
    saved = goal(role)
    source = capture(role, "https://example.org/source", "文档", "每次只改变一个参数。", "native-web:fixture")
    note = save_note(role, source["data"]["id"], source["version"], "通过控制变量定位原因。", ["每次只改变一个参数"], [])
    attached = attach(role, saved["data"]["id"], note["data"]["id"], saved["version"])
    assert attached["data"]["phase"] == "quiz"
    calls = []
    for _ in range(3):
        scheduler = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
        await step(scheduler)
    tree = status(role, saved["data"]["id"])
    assert tree["goal"]["data"]["status"] == "completed" and tree["nodes"][0]["evidence_current"]
    assert len(calls) == 3 and not role.adapter.sent
