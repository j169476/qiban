from datetime import timedelta

import pytest
from conftest import FakeHost
from test_cognition import LearningAdapter, decision, event

from bili_assistant import delivery
from bili_assistant.engine import Engine
from bili_assistant.scheduler import Scheduler


def setup_scheduler(role, monkeypatch):
    role.adapter = LearningAdapter(events=[event(role)])
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []

    async def model(*args, **kwargs):
        calls.append(True)
        return decision(action={"kind": "reply", "payload": {"event_id": "reply-1", "text": f"reply {len(calls)}"}})

    async def idle():
        pass

    monkeypatch.setattr(scheduler, "model", model)
    monkeypatch.setattr(scheduler, "maybe_reflect", idle)
    monkeypatch.setattr(scheduler, "maintenance", idle)
    return scheduler, calls


def patch_policy(role, change):
    doc = role.store.read("policy")
    change(doc["data"])
    role.store.write("policy", doc["data"], doc["version"], "test-admin")


@pytest.mark.asyncio
async def test_interval_is_deferred_and_reconsidered_once_when_due(role, monkeypatch):
    scheduler, calls = setup_scheduler(role, monkeypatch)
    role.adapter.events = [event(role, "first")]
    await role.inbox()
    prepared = role.prepare("reply", {"event_id": "first", "text": "first"})
    await role.execute(prepared["id"])
    patch_policy(role, lambda p: p.update(min_action_interval_seconds=60))
    role.adapter.events = [event(role)]
    await scheduler.cycle()
    assert not role.store.state("processed:123:reply-1")
    assert role.status()["delivery"]["pending"] == 1
    await scheduler.cycle()
    assert len(calls) == 1 and len(role.adapter.sent) == 1
    now = role.clock()
    role.clock = lambda: now + timedelta(seconds=61)
    await scheduler.cycle()
    await scheduler.cycle()
    assert len(calls) == 2 and len(role.adapter.sent) == 2
    assert role.adapter.sent[-1][1]["text"] == "reply 2"
    assert role.store.state("processed:123:reply-1")
    assert role.status()["delivery"]["pending"] == 0


@pytest.mark.asyncio
async def test_quota_deferral_survives_pause_and_next_day(role, monkeypatch):
    scheduler, calls = setup_scheduler(role, monkeypatch)
    role.adapter.events = [event(role, "first")]
    await role.inbox()
    await role.execute(role.prepare("reply", {"event_id": "first", "text": "first"})["id"])
    patch_policy(role, lambda p: p["actions"]["reply"].update(daily_limit=1))
    role.adapter.events = [event(role)]
    await scheduler.cycle()
    assert delivery.status(role.store, "123")["recent"][0]["reason"] == "quota_exhausted"
    role.stop(paused=True)
    now = role.clock()
    role.clock = lambda: now + timedelta(days=1)
    role.start()
    scheduler.generation = role.run()["generation"]
    role.adapter.events = []  # Event has left the platform's first page.
    await scheduler.cycle()
    assert len(calls) == 2 and len(role.adapter.sent) == 2
    assert role.store.state("processed:123:reply-1")


@pytest.mark.asyncio
async def test_policy_change_reconsiders_and_checks_new_permissions(role, monkeypatch):
    scheduler, calls = setup_scheduler(role, monkeypatch)
    patch_policy(role, lambda p: p["actions"]["reply"].update(daily_limit=0))
    await scheduler.cycle()
    patch_policy(role, lambda p: p["actions"]["reply"].update(enabled=False))
    await scheduler.cycle()
    assert len(calls) == 2 and not role.adapter.sent
    assert role.store.state("processed:123:reply-1")


@pytest.mark.asyncio
async def test_legacy_cancelled_reply_is_recovered_without_reusing_old_draft(role, monkeypatch):
    scheduler, calls = setup_scheduler(role, monkeypatch)
    await role.inbox()
    old = role.prepare("reply", {"event_id": "reply-1", "text": "stale draft"})
    role.store.set_state("processed:123:reply-1", True)
    role.stop(paused=True)
    role.start()
    scheduler.generation = role.run()["generation"]
    await scheduler.cycle()
    assert len(calls) == 1 and len(role.adapter.sent) == 1
    assert role.adapter.sent[0][1]["text"] != "stale draft"
    with role.store.db() as db:
        assert db.execute("SELECT status FROM actions WHERE id=?", (old["id"],)).fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_uncertain_reply_is_never_automatically_retried(role, monkeypatch):
    scheduler, calls = setup_scheduler(role, monkeypatch)
    role.adapter.fail = True
    await scheduler.cycle()
    role.stop(paused=True)
    role.start()
    scheduler.generation = role.run()["generation"]
    await scheduler.cycle()
    assert len(calls) == 1 and len(role.adapter.sent) == 1
    result = role.prepare("reply", {"event_id": "reply-1", "text": "unsafe"}, retry_unsent=True)
    assert result["status"] == "uncertain" and result["duplicate"]


def test_unsent_retry_requires_same_owner_and_explicit_flag(role):
    original = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    role.stop(paused=True)
    role.start()
    assert role.prepare("like", {"bvid": "BV1xx411c7mD"})["id"] == original["id"]
    retry = role.prepare("like", {"bvid": "BV1xx411c7mD"}, retry_unsent=True)
    assert retry["id"] != original["id"]
    other = Engine(role.store, FakeHost("another-role"), role.adapter, role.clock)
    other.start()
    assert other.prepare("like", {"bvid": "BV1xx411c7mD"}, retry_unsent=True)["id"] == retry["id"]


@pytest.mark.parametrize("reason", ["age", "attempts"])
def test_deferred_replies_have_expiry_and_attempt_bound(role, reason):
    target = event(role)
    delivery.defer(role, target, "before_send_cancelled")
    if reason == "age":
        now = role.clock()
        role.clock = lambda: now + timedelta(days=4)
    else:
        for _ in range(delivery.MAX_DEFERRALS - 1):
            delivery.defer(role, target, "before_send_cancelled")
    assert not delivery.eligible(role, target)
    assert role.store.state("processed:123:reply-1")
    assert delivery.status(role.store, "123")["recent"][0]["reason"] in ("expired", "retry_limit")
