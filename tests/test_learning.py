from copy import deepcopy

import pytest
from test_cognition import LearningAdapter, configure

from bili_assistant.api import call
from bili_assistant.conversations import append
from bili_assistant.errors import AssistantError
from bili_assistant.learning import (
    ANSWER_SCHEMA,
    GRADE_SCHEMA,
    PLAN_SCHEMA,
    QUIZ_SCHEMA,
    READ_SCHEMA,
    SELECT_SCHEMA,
    active,
    check_id,
    control,
    create,
    status,
    step,
)
from bili_assistant.scheduler import Scheduler


def request(role, content="请帮我学习最小案例调试法，重点理解控制变量及其适用边界。", key="role", message="request-1"):
    item_id = append(role.store, "123", "codex", key, message, content, "user", "", "received",
                     role.clock().isoformat())[0]
    return item_id, content


def goal(role, **changes):
    item_id, quote = request(role)
    return create(role, "学习最小案例调试法", ["能应用控制变量并说明适用边界"], item_id, quote)


class SearchAdapter(LearningAdapter):
    def __init__(self):
        super().__init__()
        self.searches = []

    async def search(self, query, limit=10):
        self.searches.append(query)
        return [{"bvid": "BV1xx411c7mD", "title": "最小案例", "owner_uid": "888", "description": ""}]


def model_stub(role, calls, fail=False, mutate=None):
    async def model(prompt, payload, schema, images=()):
        calls.append((schema, deepcopy(payload)))
        if mutate:
            mutate(schema, payload)
        if schema == PLAN_SCHEMA:
            return {"gaps": ["控制变量的应用"], "query": "最小案例 控制变量"}
        if schema == SELECT_SCHEMA:
            return {"bvid": "BV1xx411c7mD", "reason": "包含实验步骤"}
        if schema == READ_SCHEMA:
            return {"knowledge": "先运行最小案例，再修改一个参数观察结果。", "tags": ["调试"],
                    "video_topics": ["控制变量"]}
        if schema == QUIZ_SCHEMA:
            source = payload["sources"][0]
            return {"questions": [
                {"id": f"q{i}", "criterion": 0, "kind": kind,
                 "question": "如何定位新场景中的故障？" if i == 0 else "什么时候不能推广这个结论？",
                 "expected": "SECRET-REFERENCE-ANSWER", "rubric": "SECRET-RUBRIC",
                 "evidence": [{"source_id": source["id"], "quote": source["segments"][0]["text"]}]}
                for i, kind in enumerate(("transfer", "counterexample"))
            ]}
        if schema == ANSWER_SCHEMA:
            raw = str(payload)
            assert "SECRET-REFERENCE-ANSWER" not in raw and "SECRET-RUBRIC" not in raw
            assert "sources" not in payload and "evidence" not in raw
            return {"answers": [{"id": q["id"], "answer": "先保留最小案例，然后一次改变一个参数。"}
                                for q in payload["questions"]]}
        if schema == GRADE_SCHEMA:
            return {"grades": [{"id": q["id"], "passed": not fail, "reason": "核对应用与边界",
                                "gap": "未能说明边界条件" if fail else ""} for q in payload["questions"]],
                    "limitations": "同一模型分离上下文的自检，未进行独立实验"}
        raise AssertionError("Unexpected schema")
    return model


@pytest.mark.asyncio
async def test_no_automatic_goals_and_creation_requires_current_role_user_evidence(role, admin):
    role.adapter = SearchAdapter()
    scheduler = Scheduler(role, role.run()["generation"])
    request(role, "来看看这个视频，我对实验感兴趣。")
    assert await step(scheduler) is None
    assert not active(role)
    other_id, text = request(role, key="other")
    with pytest.raises(AssistantError, match="本角色聊天"):
        create(role, "知识目标", ["边界"], other_id, text)
    current_id, text = request(role, message="explicit-request")
    with pytest.raises(AssistantError, match="本角色聊天"):
        create(role, "知识目标", ["边界"], current_id, "不存在的用户原话")
    with pytest.raises(AssistantError, match="role"):
        create(admin, "知识目标", ["边界"], current_id, text)
    first = create(role, "知识目标", ["边界"], current_id, text)
    assert create(role, "知识目标", ["边界"], current_id, text)["duplicate"]
    assert role.store.state("run")["generation"] == scheduler.generation
    another, quote = request(role, message="another")
    with pytest.raises(AssistantError, match="已有一个"):
        create(role, "另一目标", ["边界"], another, quote)
    assert (await call(admin, "learning_goal_status", {}))["goals"][0]["data"] == first["data"]


@pytest.mark.asyncio
async def test_complete_pipeline_with_restart_checkpoints_and_withheld_answer_key(role, monkeypatch):
    role.adapter = SearchAdapter()
    saved = goal(role)
    calls = []
    expected_phases = ["search", "read", "quiz", "answer", "grade", "done"]
    for expected in expected_phases:
        # Rebuild scheduler every stage to demonstrate persistence rather than in-memory progress.
        scheduler = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
        await step(scheduler)
        latest = role.store.read("learning_goals", saved["data"]["id"])
        assert latest["data"]["phase"] == expected
    doc = latest["data"]
    assert doc["status"] == "completed" and doc["reason"] == "model_check_passed"
    checks = status(role, doc["id"])["checks"]
    assert len(checks) == 1 and checks[0]["data"]["passed"]
    assert checks[0]["data"]["status"] == "graded"
    assert len(calls) == 6 and len(role.adapter.searches) == 1
    assert role.adapter.read_videos == ["BV1xx411c7mD"] and not role.adapter.sent
    assert len(role.store.records("knowledge")[0]) == 1
    assert not role.store.records("interest_signals")[0]
    assert status(role, doc["id"])["goals"][0]["completion_current"]
    old = role.store.records("source_snapshots")[0][0]
    old["data"]["segments"][0]["text"] = "修改了材料"
    role.store.write("source_snapshots", old["data"], old["version"], "user", old["data"]["id"])
    state = status(role, doc["id"])
    assert not state["goals"][0]["completion_current"] and not state["checks"][0]["current"]


@pytest.mark.asyncio
async def test_failed_transfer_retains_gaps_and_pauses_at_round_limit(role, monkeypatch):
    configure(role, learning_round_limit=1)
    saved = goal(role)
    role.adapter = SearchAdapter()
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []
    monkeypatch.setattr(scheduler, "model", model_stub(role, calls, fail=True))
    for _ in range(6):
        await step(scheduler)
    latest = role.store.read("learning_goals", saved["data"]["id"])
    assert latest["data"]["status"] == "paused" and latest["data"]["round"] == 2
    assert "边界" in latest["data"]["gaps"][0]
    assert "round_limit" in latest["data"]["reason"]
    assert not status(role, saved["data"]["id"])["checks"][0]["data"]["passed"]
    assert await step(scheduler) is None
    with pytest.raises(AssistantError, match="轮数用完"):
        control(role, saved["data"]["id"], "resume", latest["version"])


@pytest.mark.asyncio
async def test_model_quota_and_goal_control_never_start_paused_assistant(role, monkeypatch):
    saved = goal(role)
    scheduler = Scheduler(role, role.run()["generation"])
    monkeypatch.setattr(scheduler, "remaining_model_calls", lambda: 0)
    before = saved["version"]
    assert (await step(scheduler))["waiting"] == "model_quota"
    assert role.store.read("learning_goals", saved["data"]["id"])["version"] == before
    role.stop(paused=True)
    paused = control(role, saved["data"]["id"], "pause", saved["version"])
    resumed = control(role, saved["data"]["id"], "resume", paused["version"])
    assert resumed["data"]["status"] == "active"
    assert role.store.state("run")["status"] == "paused"
    with pytest.raises(AssistantError):
        await step(scheduler)


@pytest.mark.asyncio
async def test_source_edits_invalidate_quiz_and_goal_edits_during_model_are_not_overwritten(role, monkeypatch):
    saved = goal(role)
    role.adapter = SearchAdapter()
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []
    monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
    for _ in range(4):
        await step(scheduler)
    source = role.store.records("source_snapshots")[0][0]
    source["data"]["segments"][0]["text"] = "人工修正材料"
    role.store.write("source_snapshots", source["data"], source["version"], "user", source["data"]["id"])
    await step(scheduler)
    latest = role.store.read("learning_goals", saved["data"]["id"])
    assert latest["data"]["phase"] == "plan" and latest["data"]["round"] == 2
    assert len(calls) == 4
    def mutate(schema, payload):
        current = role.store.read("learning_goals", saved["data"]["id"])
        current["data"]["objective"] = "人工修改的新目标"
        role.store.write("learning_goals", current["data"], current["version"], "user", current["data"]["id"])
    monkeypatch.setattr(scheduler, "model", model_stub(role, calls, mutate=mutate))
    with pytest.raises(AssistantError, match="变化"):
        await step(scheduler)
    assert role.store.read("learning_goals", saved["data"]["id"])["data"]["objective"] == "人工修改的新目标"


@pytest.mark.asyncio
async def test_crash_after_check_save_reuses_questions_instead_of_generating_again(role, monkeypatch):
    saved = goal(role)
    role.adapter = SearchAdapter()
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []
    monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
    for _ in range(4):
        await step(scheduler)
    current = role.store.read("learning_goals", saved["data"]["id"])
    current["data"].update(phase="quiz", check_ids=[])
    role.store.write("learning_goals", current["data"], current["version"], "fixture", current["data"]["id"])
    await step(scheduler)
    latest = role.store.read("learning_goals", saved["data"]["id"])
    assert latest["data"]["phase"] == "grade" and latest["data"]["check_ids"] == [check_id(latest)]
    assert len(role.store.records("learning_checks")[0]) == 1
    assert len([s for s, _ in calls if s == QUIZ_SCHEMA]) == 1


@pytest.mark.asyncio
async def test_deleted_request_pauses_goal_without_reading_or_sending(role, monkeypatch):
    saved = goal(role)
    role.store.path("conversation_turns", saved["data"]["request"]["id"]).unlink()
    scheduler = Scheduler(role, role.run()["generation"])
    result = await step(scheduler)
    assert result["waiting"] == "request_changed"
    assert role.store.read("learning_goals", saved["data"]["id"])["data"]["status"] == "paused"
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_scheduler_runs_explicit_goal_before_free_browsing_but_not_invitations(role, monkeypatch):
    role.adapter = SearchAdapter()
    saved = goal(role)
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []
    monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
    await scheduler.cycle()
    assert role.store.read("learning_goals", saved["data"]["id"])["data"]["phase"] == "search"
    assert not role.adapter.feed_modes
