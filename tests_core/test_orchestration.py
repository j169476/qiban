import asyncio
from datetime import datetime, timedelta

import pytest
from test_platform_core import Adapter, Host, Model, event

from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.observability import current_stage
from companion_core.observability import report as observation_report
from companion_core.orchestration import Flow, Stage, Workflow, run_cycle
from companion_core.runtime import Companion
from companion_core.storage import Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "core")
    result.initialize()
    return result


@pytest.mark.asyncio
async def test_two_platform_cycles_rotate_priority_and_share_model_quota(store):
    first, second = Adapter("alpha"), Adapter("beta")
    first.incoming, second.incoming = [event()], [event("beta")]
    model = Model()
    core = Companion(store, model, Host(), [first, second])
    core.clock = lambda: datetime.fromisoformat(first.incoming[0].occurred_at)
    core.start()
    initial = await core.cycle()
    assert [item["platform"] for item in initial["workflows"]] == ["alpha", "beta"]
    assert len(first.sent) == len(second.sent) == 1
    again = await core.cycle()
    assert [item["platform"] for item in again["workflows"]] == ["beta", "alpha"]
    assert len(model.calls) == 2
    assert len(first.sent) == len(second.sent) == 1
    assert core.remaining_model_calls() == store.read("policy")["data"]["daily_model_cycles"] - 2


@pytest.mark.asyncio
async def test_core_model_always_supplies_callable_phase_for_worker_cleanup(store):
    class CallbackModel:
        async def generate(self, instructions, context, schema, images=(), **options):
            options["guard"]()
            options["phase"]("cycle")
            return {}

    core = Companion(store, CallbackModel(), Host(), [])
    core.start()
    assert await core.model("", {}, {}) == {}
    phases = []
    assert await core.model("", {}, {}, phase=phases.append) == {}
    assert phases == ["cycle"]


@pytest.mark.asyncio
async def test_stage_priority_finalize_and_budget_are_enforced_by_core(store):
    trace = []
    run = {"owner": "role", "generation": "generation"}

    def stage(name, flow=Flow.CONTINUE):
        def record(state):
            assert current_stage() == name
            trace.append(name)
            return flow
        return record

    flow = Workflow("alpha", "bot", (
        Stage("collect", stage("collect"), 1, "prepare"),
        Stage("goal", stage("goal", Flow.FINALIZE), 2, requires_model=True),
        Stage("explore", stage("explore"), 3, requires_model=True),
        Stage("reflect", stage("reflect"), 4, "finalize", True),
    ))
    await run_cycle(store, [flow], guard=lambda: run, remaining_model_calls=lambda: 2)
    assert trace == ["collect", "goal", "reflect"]
    trace.clear()
    report = await run_cycle(store, [flow], guard=lambda: run, remaining_model_calls=lambda: 0)
    assert trace == ["collect"]
    assert all(item["status"] == "model_quota" for item in report["workflows"][0]["stages"][1:])
    measured = observation_report(store)
    assert {item["stage"] for item in measured["groups"]} == {"collect", "goal", "reflect"}
    assert all(item["known_tokens"]["input_tokens"] is None for item in measured["groups"])


@pytest.mark.asyncio
async def test_pause_between_stages_stops_later_work(store):
    core = Companion(store, Model(), Host(), [])
    core.start()
    called = []

    def pause(state):
        called.append("collect")
        core.stop()

    workflow = Workflow("alpha", "bot", (
        Stage("collect", pause, 1, "prepare"),
        Stage("reply", lambda state: called.append("reply"), 2),
    ))
    with pytest.raises(AssistantError) as error:
        await run_cycle(store, [workflow], guard=core.guard, remaining_model_calls=core.remaining_model_calls)
    assert error.value.code == "not_running"
    assert called == ["collect"]
    assert store.state("core-run")["status"] == "paused"


@pytest.mark.asyncio
async def test_concurrent_cycles_are_serialized(store):
    in_progress, maximum = 0, 0

    async def task(state):
        nonlocal in_progress, maximum
        in_progress += 1
        maximum = max(maximum, in_progress)
        await asyncio.sleep(0.01)
        in_progress -= 1

    workflow = Workflow("alpha", "bot", (Stage("task", task, 1),))
    async def cycle():
        return await run_cycle(store, [workflow], guard=lambda: {"owner": "role", "generation": "generation"},
                               remaining_model_calls=lambda: 1)

    await asyncio.gather(cycle(), cycle())
    assert maximum == 1


@pytest.mark.asyncio
async def test_generic_pre_send_cancellation_refunds_only_confirmed_unsent_budget(store):
    adapter, model = Adapter(), Model()
    item = event()
    now = datetime.fromisoformat(item.occurred_at)
    core = Companion(store, model, Host(), [adapter], clock=lambda: now)
    core.start()
    adapter.before = core.stop
    outcome = await core.handle(item)
    assert outcome["action"]["status"] == "cancelled"
    key = stable_key("action-budget", "alpha", "bot", "2026-10-22")
    assert store.state(key) == 0
    core.clock = lambda: now + timedelta(minutes=1)
    adapter.before = lambda: None
    core.start()
    assert (await core.handle(item))["action"]["status"] == "succeeded"
    assert store.state(key) == 1 and len(adapter.sent) == 1
