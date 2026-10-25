import asyncio
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.runtime_keys import PLATFORMS
from companion_service.service import Service
from companion_service.watchdog import pause_fault
from tests_postgres.test_runtime import Adapter, Model
from tests_postgres.test_web import PASSWORD


class Host:
    thread_id = "local-owner"
    def __init__(self):
        self.archived = set()

    def identity(self):
        return self.thread_id

    def require_active(self, thread_id=None):
        if (thread_id or self.thread_id) in self.archived:
            raise AssistantError("chat_archived", "synthetic")


def prepare(store):
    adapters = []
    for platform in PLATFORMS:
        adapter = Adapter()
        adapter.platform = platform
        adapters.append(adapter)
    service = Service(store, Model(), Host(), adapters=adapters, bridge_secret="synthetic-secret")
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    return service


def call(service, owner, name, args=None):
    return service.control("synthetic-secret", owner, name, args or {})


def test_three_distinct_commands_do_not_start_local_or_each_other(pg_store):
    service = prepare(pg_store)
    first = call(service, "bili-owner", "bili_start")["run"]
    assert service.status()["status"] == "paused"
    assert service.platform_runs.status("wechat")["status"] == "paused"
    second = call(service, "wechat-owner", "wechat_start")["run"]
    third = call(service, "qq-owner", "qq_start")["run"]
    assert len({first["generation"], second["generation"], third["generation"]}) == 3
    call(service, "wechat-owner", "wechat_stop")
    assert service.platform_runs.status("bilibili")["generation"] == first["generation"]
    assert service.platform_runs.status("bilibili")["status"] == "active"
    assert service.platform_runs.status("qq")["status"] == "active"
    with pytest.raises(AssistantError):
        call(service, "qq-owner", "bili_stop")
    call(service, "local-owner", "assistant_start")
    call(service, "local-owner", "assistant_stop")
    assert service.platform_runs.status("qq")["status"] == "active"


@pytest.mark.asyncio
async def test_archive_pauses_only_owned_platform_and_reopen_does_not_resume(pg_store):
    service = prepare(pg_store)
    call(service, "bili-owner", "bili_start")
    call(service, "qq-owner", "qq_start")
    service.host.archived.add("bili-owner")
    await service.platform_runs.check()
    assert service.platform_runs.status("bilibili")["status"] == "paused"
    assert service.platform_runs.status("qq")["status"] == "active"
    service.host.archived.clear()
    await service.platform_runs.check()
    assert service.platform_runs.status("bilibili")["status"] == "paused"


def test_unavailable_platform_does_not_change_local_run(pg_store):
    service = Service(pg_store, Model(), Host(), bridge_secret="synthetic-secret")
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    local = call(service, "local-owner", "assistant_start")["run"]
    with pytest.raises(AssistantError) as error:
        call(service, "wechat-owner", "wechat_start")
    assert error.value.code == "platform_unavailable"
    assert pg_store.state("core-run") == local


def test_repeated_start_cannot_change_mode_owner_or_extend_deadline(pg_store):
    service = prepare(pg_store)
    first = call(service, "qq-owner", "qq_start", {"duration_minutes": 1, "read_only": True})
    second = call(service, "qq-owner", "qq_start", {"duration_minutes": 100, "read_only": True})
    assert first["run"] == second["run"]
    for owner, args in [("other", {"read_only": True}), ("qq-owner", {}), ("qq-owner", {"read_only": "yes"})]:
        with pytest.raises(AssistantError):
            call(service, owner, "qq_start", args)


@pytest.mark.asyncio
async def test_outside_hours_reports_schedule_wait_without_polling(pg_store):
    service = prepare(pg_store)
    call(service, "qq-owner", "qq_start")
    service.platform_hours = lambda: False
    status = service.platform_runs.status("qq")
    assert status["status"] == "active" and status["waiting_for_schedule"]
    assert status["reason"] == "outside_active_hours" and not status["busy"]

    def outside_hours():
        service.shutdown.set()
        return False

    def forbidden_guard():
        pytest.fail("schedule wait must not start platform processing")

    service.platform_hours = outside_hours
    service.platform_runs.cores["qq"].guard = forbidden_guard
    await asyncio.wait_for(service.platform_runs.work("qq"), 3)
    assert not service.platform_runs.busy


def test_watchdog_and_restore_pause_external_slots_when_local_is_stopped(pg_store):
    service = prepare(pg_store)
    call(service, "qq-owner", "qq_start")
    assert pause_fault(pg_store, "heartbeat_lost")
    assert service.platform_runs.status("qq")["status"] == "paused"
    call(service, "qq-owner", "qq_start")
    service.admin.repository.reset_after_restore()
    assert service.platform_runs.status("qq")["status"] == "paused"


def test_only_admin_can_stop_all_slots(pg_store):
    service = prepare(pg_store)
    call(service, "admin-owner", "management_open")
    call(service, "qq-owner", "qq_start")
    call(service, "wechat-owner", "wechat_start")
    call(service, "local-owner", "assistant_start")
    with pytest.raises(AssistantError):
        call(service, "qq-owner", "assistant_stop_all")
    assert service.platform_runs.status("wechat")["status"] == "active"
    call(service, "admin-owner", "assistant_stop_all")
    assert service.status()["status"] == "paused"
    assert all(service.platform_runs.status(p)["status"] == "paused" for p in PLATFORMS)


@pytest.mark.asyncio
async def test_slow_platform_does_not_block_another_platform_receiver(pg_store):
    service = prepare(pg_store)
    service.platform_hours = lambda: True
    call(service, "bili-owner", "bili_start")
    call(service, "qq-owner", "qq_start")
    blocked, received = asyncio.Event(), asyncio.Event()

    async def slow_poll():
        blocked.set()
        await asyncio.Event().wait()

    async def quick_poll():
        received.set()
        return []

    async def no_cycle():
        return None

    manager = service.platform_runs
    manager.cores["bilibili"].poll = slow_poll
    manager.cores["qq"].poll = quick_poll
    manager.cores["qq"].cycle = no_cycle
    manager.open()
    try:
        await asyncio.wait_for(asyncio.gather(blocked.wait(), received.wait()), 5)
        call(service, "bili-owner", "bili_stop")
        assert manager.status("qq")["status"] == "active"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_public_events_cannot_fill_the_private_reply_batch(pg_store):
    service = prepare(pg_store)
    service.platform_hours = lambda: True
    call(service, "qq-owner", "qq_start")
    core, handled = service.platform_runs.cores["qq"], []
    private = SimpleNamespace(visibility="private")

    async def poll():
        return [*[SimpleNamespace(visibility="public") for _ in range(3)], private]

    async def handle(event):
        handled.append(event)
        service.shutdown.set()

    async def no_cycle():
        return None

    core.poll, core.handle, core.cycle = poll, handle, no_cycle
    await asyncio.wait_for(service.platform_runs.work("qq"), 5)
    assert handled == [private]
