"""Regression coverage for the second review's runtime and takeover failures."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from test_core_takeover import NOW, Platform, incoming, make

from bili_assistant.connector import normalize
from bili_assistant.scheduler import Scheduler
from companion_core import budget, delivery, events
from companion_core.errors import AssistantError


def policy_update(store, **values):
    record = store.read("policy")
    record["data"].update(values)
    store.write("policy", record["data"], record["version"], "test")


def test_default_video_resource_type_does_not_change_legacy_event_identity():
    old = incoming(kind="reply")
    assert normalize("123", old).as_dict() == normalize("123", {**old, "resource_type": 1}).as_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_first", [True, False])
async def test_model_budget_survives_both_controller_switches(store, legacy_first):
    core, _, legacy, platform, model = make(store)
    policy_update(store, daily_model_cycles=1)
    platform.incoming = [incoming()]
    if legacy_first:
        legacy.start()
        store.set_state("model-cycles:123:2026-10-22", 1)
        assert Scheduler(legacy, store.state("run")["generation"]).remaining_model_calls() == 0
        legacy.stop(paused=True)
        core.start()
        event = (await core.poll())[0]
        with pytest.raises(AssistantError, match="当日模型额度") as error:
            await core.handle(event)
        assert error.value.code == "model_quota"
        assert not model.calls and not platform.sent
        assert store.state(delivery.key(event))["reason"] == "model_quota"
    else:
        core.start()
        assert (await core.handle((await core.poll())[0]))["action"]["status"] == "succeeded"
        core.stop()
        legacy.start()
        scheduler = Scheduler(legacy, store.state("run")["generation"])
        assert scheduler.remaining_model_calls() == 0
        with pytest.raises(AssistantError) as error:
            await scheduler.model("must not call real model", {}, {})
        assert error.value.code == "model_quota"
        assert len(model.calls) == 1


def test_historical_budget_is_merged_once_and_new_reservations_are_not_reimported(store):
    policy_update(store, daily_model_cycles=5)
    policy = store.read("policy")["data"]
    store.set_state("model-cycles:123:2026-10-22", 1)
    store.set_state("core-model:2026-10-22", 1)
    assert [budget.remaining(store, policy, NOW) for _ in range(3)] == [3, 3, 3]
    budget.reserve(store, policy, NOW, account="123")
    budget.reserve(store, policy, NOW)
    assert [budget.remaining(store, policy, NOW) for _ in range(3)] == [1, 1, 1]
    assert store.state("model-cycles:123:2026-10-22") == 2
    assert store.state("core-model:2026-10-22") == 2
    # Changing the bound account cannot produce another daily allowance.
    budget.reserve(store, policy, NOW, account="456")
    assert budget.remaining(store, policy, NOW) == 0
    assert budget.remaining(store, policy, NOW + timedelta(days=1)) == 5


def test_status_reports_all_controller_and_account_usage_without_mutating_budget(store):
    _, _, legacy, _, _ = make(store)
    legacy.start()
    policy_update(store, daily_model_cycles=5)
    store.set_state("model-cycles:123:2026-10-22", 1)
    store.set_state("model-cycles:456:2026-10-22", 2)
    store.set_state("core-model:2026-10-22", 1)
    quota = legacy.status()["model_quota"]
    assert quota == {"day": "2026-10-22", "scope": "data_space", "used": 4, "limit": 5, "remaining": 1}
    assert store.state("model-budget:2026-10-22") is None
    assert Scheduler(legacy, legacy.run()["generation"]).remaining_model_calls() == quota["remaining"]


class YieldingPlatform(Platform):
    async def me(self):
        await asyncio.sleep(0.01)
        return await super().me()


@pytest.mark.asyncio
@pytest.mark.parametrize("without_task_lock", [False, True])
async def test_same_native_action_is_sent_once_even_for_two_core_events(store, monkeypatch, without_task_lock):
    if without_task_lock:
        @asynccontextmanager
        async def bypass_task_lock(*args):
            yield

        monkeypatch.setattr("companion_core.execution.action_lock", bypass_task_lock)
    platform = YieldingPlatform()
    core, _, _, _, _ = make(store, platform=platform)
    policy = store.read("policy")
    policy["data"]["actions"]["comment"].update(enabled=True, daily_limit=10)
    store.write("policy", policy["data"], policy["version"], "test")
    core.start()
    platform.incoming = [incoming(native="dm-789-a"), incoming(native="dm-789-b")]
    first, second = await core.poll()
    proposal = {"capability": "bilibili.comment", "arguments": {"bvid": "BV1RT4y1h7YY", "text": "同一个视频动作"}}
    outcomes = await asyncio.gather(core.dispatch(first, proposal), core.dispatch(second, proposal))
    assert all(result["status"] == "succeeded" for result in outcomes)
    assert len(platform.sent) == 1
    with store.db() as db:
        assert db.execute("SELECT COUNT(*),SUM(units) FROM actions").fetchone()[:] == (1, 1)


@pytest.mark.asyncio
async def test_interval_deferral_reconsiders_when_due_without_repeating_relationship_update(store):
    core, connector, _, platform, model = make(store)
    policy_update(store, min_action_interval_seconds=300)
    model.relationship = {"delta": 1, "impression": "讨论设计", "facts": []}
    core.start()
    platform.incoming = [incoming(native="dm-first"), incoming(native="dm-second")]
    first, second = await core.poll()
    assert (await core.handle(first))["action"]["status"] == "succeeded"
    assert (await core.handle(second))["action"]["error"] == "interval"
    assert store.state("core-processed:" + second.id) is None
    assert store.state(delivery.key(second))["status"] == "deferred"
    assert await core.poll() == []
    assert (await core.handle(second))["deferred"]
    assert len(model.calls) == 2
    core.clock = connector.engine.clock = lambda: NOW + timedelta(minutes=6)
    assert [event.id for event in await core.poll()] == [second.id]
    assert (await core.handle(second))["action"]["status"] == "succeeded"
    assert len(platform.sent) == 2
    assert len(model.calls) == 3
    assert store.read("relationships", "789")["data"]["interactions"] == 2
    assert await core.poll() == []


@pytest.mark.asyncio
async def test_daily_quota_deferral_reconsiders_next_day(store):
    core, connector, _, platform, _ = make(store)
    policy = store.read("policy")
    policy["data"]["actions"]["dm_reply"]["daily_limit"] = 1
    store.write("policy", policy["data"], policy["version"], "test")
    core.start()
    platform.incoming = [incoming(native="first"), incoming(native="second")]
    first, second = await core.poll()
    await core.handle(first)
    assert (await core.handle(second))["action"]["error"] == "quota_exhausted"
    assert await core.poll() == []
    core.clock = connector.engine.clock = lambda: NOW + timedelta(days=1)
    assert [event.id for event in await core.poll()] == [second.id]
    assert (await core.handle(second))["action"]["status"] == "succeeded"
    assert len(platform.sent) == 2


@pytest.mark.asyncio
async def test_pause_before_send_preserves_event_until_explicit_restart(store):
    core, connector, _, platform, _ = make(store)
    core.start()
    platform.incoming = [incoming()]
    event = (await core.poll())[0]
    platform.before = core.stop
    assert (await core.handle(event))["action"]["status"] == "cancelled"
    assert not platform.sent
    assert store.state("core-run")["status"] == "paused"
    assert store.state(delivery.key(event))["status"] == "deferred"
    with pytest.raises(AssistantError):
        await core.poll()
    core.clock = connector.engine.clock = lambda: NOW + timedelta(minutes=1)
    platform.before = lambda: None
    core.start()
    assert (await core.handle((await core.poll())[0]))["action"]["status"] == "succeeded"
    assert len(platform.sent) == 1


@pytest.mark.asyncio
async def test_pause_after_reservation_keeps_explicit_unsent_receipt_for_restart(store, monkeypatch):
    core, connector, _, platform, _ = make(store)
    core.start()
    platform.incoming = [incoming()]
    event = (await core.poll())[0]

    def stopped(run):
        core.stop("test_after_reservation")
        return True

    original = connector.engine.execution_stopped
    monkeypatch.setattr(connector.engine, "execution_stopped", stopped)
    result = await core.handle(event)
    assert result["action"]["status"] == "cancelled" and result["action"]["error"] == "before_send_cancelled"
    assert not platform.sent and store.state("core-processed:" + event.id) is None
    assert store.state(delivery.key(event))["status"] == "deferred"
    assert store.state("core-run")["status"] == "paused"
    monkeypatch.setattr(connector.engine, "execution_stopped", original)
    core.clock = connector.engine.clock = lambda: NOW + timedelta(minutes=1)
    core.start()
    assert (await core.handle((await core.poll())[0]))["action"]["status"] == "succeeded"
    assert len(platform.sent) == 1


@pytest.mark.asyncio
async def test_deferred_events_do_not_hide_new_conversations_before_limit(store):
    core, _, _, platform, _ = make(store)
    run = core.start()
    originals = [incoming(native=f"deferred-{i}") for i in range(105)]
    platform.incoming = originals
    await core.poll()
    from bili_assistant.connector import normalize

    for original in originals:
        event = normalize("123", original)
        delivery.defer(core, event, events.get(store, event.id)["version"], run, "quota_exhausted")
    fresh = incoming(native="fresh-after-backlog")
    fresh["received"] = (NOW + timedelta(seconds=1)).isoformat()
    platform.incoming.append(fresh)
    assert [event.native_id for event in await core.poll()] == [fresh["id"]]


@pytest.mark.asyncio
async def test_old_completion_or_deferral_cannot_overwrite_a_corrected_event_receipt(store):
    core, _, _, platform, _ = make(store)
    run = core.start()
    platform.incoming = [incoming()]
    old = (await core.poll())[0]
    old_version = events.get(store, old.id)["version"]
    corrected = replace(old, text="更正后的原始消息")
    new_version = events.ingest(store, corrected)["version"]
    delivery.finish(core, corrected, new_version, run)
    assert delivery.defer(core, old, old_version, run, "interval")["status"] == "superseded"
    assert delivery.finish(core, old, old_version, run)["status"] == "superseded"
    assert store.state("core-processed:" + old.id) == new_version
    assert store.state(delivery.key(old))["version"] == new_version


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [False, True])
async def test_auto_relationship_switch_or_lock_does_not_block_reply(store, locked):
    core, _, _, platform, model = make(store)
    if locked:
        store.write("relationships", {"schema_version": 1, "id": "789", "display_name": "固定印象",
                    "affinity": 50, "interactions": 0, "events": [], "locked": True}, None, "test", "789")
        before = store.read("relationships", "789")["version"]
    else:
        companion = store.read("companion")
        companion["data"]["auto_relationships"] = False
        store.write("companion", companion["data"], companion["version"], "test")
    model.relationship = {"delta": 1, "impression": "自动印象", "facts": ["自动事实"]}
    core.start()
    platform.incoming = [incoming()]
    assert (await core.handle((await core.poll())[0]))["action"]["status"] == "succeeded"
    assert len(model.calls) == len(platform.sent) == 1
    if locked:
        assert store.read("relationships", "789")["version"] == before
    else:
        assert not store.path("relationships", "789").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["companion", "preferences"])
async def test_configuration_change_during_decision_invalidates_before_writes(store, kind):
    core, _, _, platform, model = make(store)
    original = model.generate

    async def change(*args, **kwargs):
        result = await original(*args, **kwargs)
        document = store.read(kind)
        if kind == "companion":
            document["data"]["auto_relationships"] = False
        else:
            document["data"]["blocked_uids"] = ["789"]
        store.write(kind, document["data"], document["version"], "test")
        return result

    model.generate = change
    model.relationship = {"delta": 1, "impression": "不可写入", "facts": []}
    core.start()
    platform.incoming = [incoming()]
    with pytest.raises(AssistantError) as error:
        await core.handle((await core.poll())[0])
    assert error.value.code == "context_changed"
    assert not platform.sent
    assert not store.path("relationships", "789").exists()
