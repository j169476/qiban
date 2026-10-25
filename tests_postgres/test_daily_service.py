import asyncio
from datetime import timedelta

import pytest

from companion_core import action_budget, events
from companion_core.contracts import Event
from tests_postgres.test_service_controls import prepared
from tests_postgres.test_web import PASSWORD


def started(store, **policy):
    service = prepared(store)
    current = store.read("policy")
    store.write("policy", {**current["data"], **policy}, current["version"], "fixture")
    token = service.sessions.login(PASSWORD, "role")
    service.control("synthetic-bridge-secret", "control", "assistant_start", {"background": False})
    return service, token


async def cycle(service):
    previous = service.maintain_local

    async def finish():
        result = await previous()
        service.shutdown.set()
        return result

    service.maintain_local = finish
    service.wakeup.set()
    try:
        await asyncio.wait_for(service.work(), 15)
    finally:
        service.maintain_local = previous
        service.shutdown.clear()


@pytest.mark.asyncio
async def test_local_replies_do_not_spend_or_obey_external_action_allowance(pg_store):
    service, token = started(pg_store, daily_action_limit=0)
    for index in range(2):
        service.local.post(token, "synthetic", f"{index:032x}")
    await cycle(service)
    assert len(service.core.decisions.model.contexts) == 2
    assert action_budget.status(service.core, service.local)["used"] == 2
    assert service.status()["status"] == "active"
    assert not pg_store.state("service-last-error")


@pytest.mark.asyncio
async def test_local_quota_waits_without_model_call_and_config_change_wakes_message(pg_store):
    service, token = started(pg_store, daily_local_replies=1)
    service.local.post(token, "first", "a" * 32)
    second = service.local.post(token, "second", "b" * 32)
    await cycle(service)
    assert len(service.core.decisions.model.contexts) == 1
    assert service.status()["status"] == "active"
    assert pg_store.state("web-message:" + second["id"])["reason"] == "quota_exhausted"
    assert not pg_store.state("service-last-error")
    await service.drain_local()
    assert len(service.core.decisions.model.contexts) == 1
    policy = pg_store.read("policy")
    pg_store.write("policy", {**policy["data"], "daily_local_replies": 2}, policy["version"], "fixture")
    await service.drain_local()
    await service.drain_local()
    assert len(service.core.decisions.model.contexts) == 2
    event = Event.from_dict(events.get(pg_store, second["id"])["event"])
    assert (await service.core.handle(event))["duplicate"]


@pytest.mark.asyncio
async def test_local_quota_wakes_next_day_without_restarting_role(pg_store):
    service, token = started(pg_store, daily_local_replies=1)
    service.local.post(token, "first", "a" * 32)
    service.local.post(token, "second", "b" * 32)
    await cycle(service)
    now = service.core.clock()
    service.core.clock = lambda: now + timedelta(days=1)
    await service.drain_local()
    assert len(service.core.decisions.model.contexts) == 2
    assert action_budget.status(service.core, service.local)["used"] == 1
    assert service.status()["status"] == "active"


@pytest.mark.asyncio
async def test_model_quota_keeps_message_pending_without_pausing(pg_store):
    service, token = started(pg_store, daily_model_cycles=0, daily_local_replies=10)
    received = service.local.post(token, "pending", "a" * 32)
    await cycle(service)
    assert not service.core.decisions.model.contexts
    assert not pg_store.state("core-processed:" + received["id"])
    assert service.status()["status"] == "active"
