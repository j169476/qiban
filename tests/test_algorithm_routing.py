"""Actual Bilibili orchestration uses the independent core router."""
import pytest
from test_cognition import DECISION_SCHEMA, PLAN_SCHEMA, configure, decision, material

from bili_assistant.scheduler import Scheduler
from companion_core.observability import report


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", [True, False])
async def test_cycle_uses_algorithm_for_clear_choice_and_model_for_unknown(role, monkeypatch, clear):
    configure(role, daily_reflection=False, memory_maintenance=False, search_expansion=False)
    item = material()
    item["title"] = "动画分镜制作" if clear else "未知题材的一天"
    schemas = []

    async def inbox(since):
        return []

    async def feed(**kwargs):
        return [item]

    async def read(bvid, page_index=0):
        return item

    async def model(prompt, context, schema, images=()):
        schemas.append(schema)
        if schema == PLAN_SCHEMA:
            return {"bvid": item["bvid"], "reason": "固定的语义回退", "memory_refs": []}
        assert schema == DECISION_SCHEMA
        return decision()

    role.adapter.inbox, role.adapter.feed, role.adapter.video_context = inbox, feed, read
    scheduler = Scheduler(role, role.run()["generation"])
    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert schemas == ([DECISION_SCHEMA] if clear else [PLAN_SCHEMA, DECISION_SCHEMA])
    assert not role.adapter.sent
    assert role.store.state("selections:123") == 1
    routes = [g for g in report(role.store)["groups"] if g["kind"] == "routing"]
    assert any(("algorithm.clear_match" if clear else "model.semantic_uncertainty") in g["stage"] for g in routes)


@pytest.mark.asyncio
async def test_one_remaining_call_can_read_clear_choice_without_spending_it_on_selection(role, monkeypatch):
    configure(role, daily_reflection=False, memory_maintenance=False, search_expansion=False)
    item = {**material(), "title": "动画分镜"}
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []

    async def inbox(since):
        return []

    async def feed(**kwargs):
        return [item]

    async def read(bvid, page_index=0):
        return item

    async def model(prompt, context, schema, images=()):
        calls.append(schema)
        assert schema == DECISION_SCHEMA
        return decision()

    role.adapter.inbox, role.adapter.feed, role.adapter.video_context = inbox, feed, read
    monkeypatch.setattr(scheduler, "remaining_model_calls", lambda: 1 if not calls else 0)
    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert calls == [DECISION_SCHEMA]


@pytest.mark.asyncio
async def test_background_expansion_cache_is_wired_and_exposes_late_translated_evidence(role, monkeypatch, memory):
    from test_retrieval import add

    from companion_core.memory_maintenance import RECALL_SCHEMA

    configure(role, catalog_limit=10, context_char_limit=6000)
    fact = "Heterodyne shot noise requires independent calibration."
    add(role.store, memory, "target", "一般背景资料\n" * 800 + fact)
    for i in range(10):
        add(role.store, memory, f"noise-{i}", "无关的日常讨论")
    calls = []

    async def model(prompt, context, schema, images=()):
        calls.append(schema)
        if schema == RECALL_SCHEMA:
            return {"queries": ["heterodyne shot noise"]}
        target = next(r for r in context["memory"]["records"] if r["data"]["id"] == "target")
        assert fact in target["data"]["content"]
        return decision()

    scheduler = Scheduler(role, role.run()["generation"])
    monkeypatch.setattr(scheduler, "model", model)
    for i in range(2):
        await scheduler.decide({"events": [{"id": f"dm-{i}", "kind": "dm_reply", "uid": "789",
                                            "text": "光学测量里的噪声怎么办"}]})
    assert calls == [RECALL_SCHEMA, DECISION_SCHEMA, DECISION_SCHEMA]


@pytest.mark.asyncio
async def test_explicit_learning_uses_core_selection_and_can_revisit_ordinary_history(role, monkeypatch):
    from test_learning import PLAN_SCHEMA as GOAL_PLAN
    from test_learning import SearchAdapter, active, goal, model_stub, step

    from bili_assistant.discovery import observe

    role.adapter = SearchAdapter()
    item = {**material(), "title": "最小案例 控制变量"}

    async def search(query, limit=10):
        return [item]

    role.adapter.search = search
    observe(role, item, "read", "following", ["控制变量"])
    saved = goal(role)
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []
    monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
    await step(scheduler)
    await step(scheduler)
    assert [schema for schema, _ in calls] == [GOAL_PLAN]
    current = active(role)
    assert current["data"]["id"] == saved["data"]["id"]
    assert current["data"]["phase"] == "read" and current["data"]["selected_bvid"] == item["bvid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "policy"])
async def test_algorithm_choice_still_checks_host_and_config_before_read(role, monkeypatch, change):
    from bili_assistant import algorithm_inputs
    from companion_core.errors import AssistantError

    configure(role, daily_reflection=False, memory_maintenance=False, search_expansion=False)
    item = {**material(), "title": "动画分镜"}
    original = algorithm_inputs.choose
    reads = []

    async def inbox(since):
        return []

    async def feed(**kwargs):
        return [item]

    async def read(bvid, page_index=0):
        reads.append(bvid)
        return item

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "archive":
            role.host.states["role"] = True
        else:
            configure(role, select_by_interest=False)
        return result

    role.adapter.inbox, role.adapter.feed, role.adapter.video_context = inbox, feed, read
    monkeypatch.setattr(algorithm_inputs, "choose", interrupted)
    scheduler = Scheduler(role, role.run()["generation"])
    with pytest.raises(AssistantError):
        await scheduler.cycle()
    assert not reads and not role.adapter.sent
