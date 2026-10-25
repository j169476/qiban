import asyncio
from datetime import timedelta

import pytest
from test_cognition import LearningAdapter, event

from bili_assistant import delivery
from bili_assistant.errors import AssistantError
from bili_assistant.health import faults
from bili_assistant.inbox_worker import InboxWorker
from bili_assistant.scheduler import Scheduler


@pytest.mark.asyncio
async def test_collector_journals_without_model_quota_and_deduplicates(role):
    role.adapter = LearningAdapter(events=[event(role)])
    generation = role.run()["generation"]
    role.store.set_state("model-cycles:123:2026-10-20", 10000)
    worker = InboxWorker(role, generation)
    await worker.poll()
    assert worker.wake.is_set()
    assert worker.values["received"] == 1
    assert not role.store.records("conversation_turns")[0]  # YAML work belongs to the consumer.
    worker.wake.clear()
    await worker.poll()
    assert not worker.wake.is_set() and worker.values["received"] == 1
    scheduler = Scheduler(role, generation)
    scheduler.collector = worker
    await scheduler.cycle()
    assert len(role.store.records("conversation_turns")[0]) == 1
    assert not role.store.state("processed:123:reply-1")
    assert role.store.state("model-cycles:123:2026-10-20") == 10000
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_daemon_receives_while_consumer_is_busy_and_stops_both(role, monkeypatch):
    import bili_assistant.scheduler as module

    generation = role.run()["generation"]
    role.adapter = LearningAdapter(events=[event(role)])
    entered, persisted = asyncio.Event(), asyncio.Event()
    scheduler = Scheduler(role, generation)
    original = InboxWorker.poll

    async def poll(worker):
        await entered.wait()
        result = await original(worker)
        persisted.set()
        return result

    async def slow_cycle():
        entered.set()
        await asyncio.wait_for(persisted.wait(), timeout=3)
        with role.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert not role.adapter.sent
        role.stop(paused=True)

    monkeypatch.setattr(module, "readiness", lambda engine: {"ready": True})
    monkeypatch.setattr(InboxWorker, "poll", poll)
    monkeypatch.setattr(scheduler, "cycle", slow_cycle)
    await asyncio.wait_for(scheduler.run(), timeout=5)
    assert role.store.state("run")["status"] == "paused"
    assert role.store.state(f"inbox:{generation}")["phase"] == "stopped"
    assert not faults(role.store) and scheduler.collector is None


@pytest.mark.asyncio
async def test_late_network_response_after_archive_is_not_persisted(role):
    generation = role.run()["generation"]
    started, release = asyncio.Event(), asyncio.Event()

    async def incoming(since):
        started.set()
        await release.wait()
        return [event(role)]

    role.adapter.inbox = incoming
    worker = InboxWorker(role, generation)
    task = asyncio.create_task(worker.poll())
    await started.wait()
    role.host.states["role"] = True
    release.set()
    with pytest.raises(AssistantError, match="archived"):
        await task
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert role.store.state("run")["status"] == "paused"


@pytest.mark.asyncio
async def test_transient_inbox_failures_back_off_then_pause_once(role, monkeypatch):
    generation = role.run()["generation"]
    worker = InboxWorker(role, generation)
    waits = []
    sleep = asyncio.sleep

    async def broken(since):
        raise AssistantError("read_timeout", "fixture")

    async def fast_wait(seconds):
        waits.append(seconds)
        assert not faults(role.store)
        await sleep(0)

    role.adapter.inbox = broken
    monkeypatch.setattr(asyncio, "sleep", fast_wait)
    await worker.run()
    assert waits == [120, 240]
    assert worker.values["failures"] == 3
    assert role.store.state("run")["reason"] == "inbox_repeated_failure"
    assert len(faults(role.store)) == 1
    assert worker.values["phase"] == "stopped"


@pytest.mark.asyncio
async def test_invalid_credentials_pause_immediately_and_do_not_start_role(role):
    async def broken(since):
        raise AssistantError("login_required", "fixture")

    role.adapter.inbox = broken
    worker = InboxWorker(role, role.run()["generation"])
    await worker.run()
    assert role.store.state("run")["reason"] == "login_required"
    assert worker.values["failures"] == 1
    before = role.store.state("run")
    role.status()
    assert role.store.state("run") == before


@pytest.mark.asyncio
async def test_outside_hours_never_contacts_platform(role):
    now = role.clock()
    role.clock = lambda: now + timedelta(hours=12)

    async def forbidden(since):
        pytest.fail("outside active hours")

    role.adapter.inbox = forbidden
    worker = InboxWorker(role, role.run()["generation"])
    await worker.poll()
    assert worker.values["wait_reason"] == "outside_hours"


@pytest.mark.asyncio
async def test_read_only_does_not_expire_or_complete_deferred_replies(role):
    target = event(role)
    role.adapter = LearningAdapter(events=[target])
    delivery.defer(role, target, "before_send_cancelled")
    role.stop(paused=True)
    now = role.clock()
    role.clock = lambda: now + timedelta(days=4)
    role.start(read_only=True)
    role.store.set_state("model-cycles:123:2026-10-24", 10000)
    await Scheduler(role, role.run()["generation"]).cycle()
    assert not role.store.state("processed:123:reply-1")
    assert delivery.status(role.store, "123")["pending"] == 1


@pytest.mark.asyncio
async def test_new_process_recovers_journaled_events_once(role):
    from bili_assistant.conversations import recover_platform_turns
    from bili_assistant.engine import Engine
    from bili_assistant.storage import Store

    role.adapter = LearningAdapter(events=[event(role)])
    await InboxWorker(role, role.run()["generation"]).poll()
    role.stop(paused=True)
    reopened = Engine(Store(role.store.root), role.host, role.adapter, role.clock)
    recover_platform_turns(reopened)
    recover_platform_turns(reopened)
    assert len(reopened.store.records("conversation_turns")[0]) == 1
    assert reopened.store.state("run")["status"] == "paused"
    assert not reopened.adapter.sent
