from datetime import timedelta

import pytest
from test_cognition import BVID, LearningAdapter, configure, decision, event

from bili_assistant.cognition import PLAN_SCHEMA, packet
from bili_assistant.errors import AssistantError
from bili_assistant.health import fail, faults
from bili_assistant.scheduler import Scheduler


def test_deadline_pauses_without_fault_or_extending_on_repeated_start(role):
    role.stop(paused=True)
    start = role.clock()
    run = role.start(duration_minutes=1440, read_only=True)["run"]
    generation = run["generation"]
    role.clock = lambda: start + timedelta(hours=23)
    assert role.start()["run"]["deadline_at"] == run["deadline_at"]
    assert role.start(duration_minutes=1440, read_only=True)["reused"]
    with pytest.raises(AssistantError, match="先暂停"):
        role.start(read_only=False)
    role.clock = lambda: start + timedelta(days=1)
    with pytest.raises(AssistantError) as exc:
        role.run()
    assert exc.value.code == "duration_complete"
    # A supervisor/worker observing the other process's normal stop must not overwrite it.
    fail(role, "not_running", generation)
    assert role.status()["run"]["reason"] == "duration_complete"
    assert not faults(role.store)
    assert role.load()["run"]["status"] == "paused"
    assert role.store.state("run")["generation"] == generation


def test_read_status_expires_foreground_run_without_starting_anything(role):
    role.stop(paused=True)
    before = role.clock()
    run = role.start(duration_minutes=1)["run"]
    role.clock = lambda: before + timedelta(minutes=2)
    status = role.status()
    assert status["run"]["status"] == "paused"
    assert status["run"]["reason"] == "duration_complete"
    assert status["run"]["generation"] == run["generation"]
    assert not status["health"]["faults"]


@pytest.mark.parametrize("duration", [0, 1441, True, 1.5])
def test_invalid_duration_does_not_change_run(role, duration):
    before = role.store.state("run")
    with pytest.raises(AssistantError) as exc:
        role.start(duration_minutes=duration)
    assert exc.value.code == "invalid_duration"
    assert role.store.state("run") == before


@pytest.mark.asyncio
async def test_read_only_blocks_preparation_and_dispatch_without_changing_policy(role):
    policy = role.store.read("policy")
    prepared = role.prepare("like", {"bvid": BVID})
    # Cover dispatch of a preparation made before the read-only restriction was recorded.
    run = role.run()
    run["read_only"] = True
    role.store.set_state("run", run)
    with pytest.raises(AssistantError) as exc:
        role.prepare("coin", {"bvid": BVID, "units": 1})
    assert exc.value.code == "action_disabled"
    with pytest.raises(AssistantError) as exc:
        await role.execute(prepared["id"])
    assert exc.value.code == "action_disabled"
    assert not role.adapter.sent
    assert role.status()["quota"]["like"]["used"] == 0
    assert role.store.read("policy") == policy
    assert packet(role, {})[0]["runtime"]["read_only"]


@pytest.mark.asyncio
async def test_read_only_cycles_keep_invitations_pending_and_save_actual_learning(role, monkeypatch):
    role.stop(paused=True)
    run = role.start(read_only=True)["run"]
    incoming = event(role)
    role.adapter = LearningAdapter([incoming], [(BVID, "最小案例学习法")])
    configure(role, search_expansion=False, daily_reflection=False, memory_maintenance=False)
    scheduler = Scheduler(role, run["generation"])

    async def model(instructions, context, schema, images=()):
        assert context["runtime"]["read_only"]
        assert not context["material"]["events"]
        if schema == PLAN_SCHEMA:
            return {"bvid": BVID, "reason": "读取案例", "memory_refs": []}
        return decision(
            knowledge="先运行最小案例，再修改一个参数观察结果。", tags=["案例"],
            action={"kind": "like", "payload": {
                "bvid": BVID, "text": "", "uid": "", "event_id": "", "units": 1,
            }},
        )

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert not role.adapter.sent
    assert role.store.state(f"learned:123:{BVID}")
    assert not role.store.state("processed:123:reply-1")
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE id='reply-1'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    role.stop(paused=True)
    assert role.start()["run"]["pending_since"] == run["pending_since"]


def test_archive_stop_is_quiet_even_when_worker_finishes_later(role):
    run = role.run()
    role.host.states["role"] = True
    with pytest.raises(AssistantError) as exc:
        role.run()
    assert exc.value.code == "chat_archived"
    fail(role, exc.value.code, run["generation"])
    assert role.store.state("run")["reason"] == "chat_archived"
    assert not faults(role.store)
