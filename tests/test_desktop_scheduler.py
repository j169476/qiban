"""Independent desktop loops and cancellation, without network or model requests."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_scheduler import DesktopScheduler
from qiban.desktop_tasks import TaskBarrier


class Store:
    def __init__(self):
        self.runs = {p: {"status": "active", "generation": p + "-1"} for p in ("qq", "wechat")}

    def state(self, key, default=None):
        return dict(self.runs[key.split(":")[-1]])

    def read(self, kind):
        return {
            "data": {
                "inbox_poll_seconds": 0.02,
                "inbox_timeout_seconds": 1,
                "inbox_batch_size": 3,
                "cycle_minutes": 0.001,
            }
        }


class Core:
    def __init__(self, platform, store):
        self.platform, self.store = platform, store
        self.run_key = "core-run:" + platform
        self.platforms = SimpleNamespace(all=lambda: [SimpleNamespace(poll_interval_seconds=0.02)])
        self.polls = self.cycles = 0
        self.handled = []

    def guard(self, generation=None):
        run = self.store.state(self.run_key)
        if run["status"] != "active":
            raise AssistantError("not_running", "Stopped")
        if generation and run["generation"] != generation:
            raise AssistantError("stale_generation", "Replaced")
        return run

    def remaining_model_calls(self):
        return 10

    async def poll(self):
        self.polls += 1
        return []

    async def cycle(self):
        self.cycles += 1

    async def handle(self, event):
        self.handled.append(event.id)


def runtime():
    store = Store()
    value = SimpleNamespace(store=store, tasks=TaskBarrier(), errors=[])
    value.cores = {p: Core(p, store) for p in store.runs}

    def fail(platform, code, generation):
        run = store.runs[platform]
        if run["status"] == "active" and run["generation"] == generation:
            run["status"] = "paused"
            value.errors.append((platform, code))

    value.fail = fail
    return value


async def until(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.005)


async def stop(scheduler, task):
    scheduler.stop_requested.set()
    await asyncio.wait_for(task, 3)


async def test_stopped_platform_can_be_added_and_removed_without_restarting_other_loop():
    value = runtime()
    value.cores.pop("qq")
    scheduler = DesktopScheduler(value, tick=0.005)
    task = asyncio.create_task(scheduler._run())
    await until(lambda: value.cores["wechat"].polls >= 2)
    other = value.cores["wechat"]
    added = Core("qq", value.store)
    await asyncio.to_thread(scheduler.replace_platform, "qq", lambda: value.cores.update(qq=added))
    await until(lambda: added.polls > 0)
    count = other.polls
    await asyncio.to_thread(scheduler.replace_platform, "qq", lambda: value.cores.pop("qq"))
    await until(lambda: other.polls > count)
    assert not value.errors and not scheduler.failure
    await stop(scheduler, task)


async def test_replacement_waits_for_old_idle_cleanup_while_other_platform_progresses():
    value = runtime()
    value.store.runs["qq"]["status"] = "paused"
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def idle():
        entered.set()
        await release.wait()

    async def close():
        closed.append(True)

    value.cores["qq"].platforms = SimpleNamespace(all=lambda: [SimpleNamespace(idle=idle, close=close)])
    scheduler = DesktopScheduler(value, tick=0.005)
    task = asyncio.create_task(scheduler._run())
    await entered.wait()
    before = value.cores["wechat"].polls
    replacement = Core("qq", value.store)
    changing = asyncio.create_task(asyncio.to_thread(scheduler.replace_platform, "qq", lambda: value.cores.update(qq=replacement)))
    await until(lambda: value.cores["wechat"].polls > before + 1)
    assert not changing.done() and value.cores["qq"] is not replacement
    release.set()
    await changing
    assert closed == [True] and value.cores["qq"] is replacement
    await stop(scheduler, task)


async def test_long_platform_does_not_block_other_platform_and_exit_drains_cleanup():
    value = runtime()
    entered, cleaned = threading.Event(), threading.Event()

    async def slow():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            cleaned.set()

    value.cores["qq"].poll = slow
    scheduler = DesktopScheduler(value, tick=0.005)
    task = asyncio.create_task(scheduler._run())
    await until(lambda: entered.is_set() and value.cores["wechat"].polls >= 2)
    assert scheduler.status("qq")["busy"]
    await stop(scheduler, task)
    assert cleaned.is_set() and not value.tasks.attempts


async def test_stop_cancels_only_its_platform_and_repeated_stop_preserves_cleanup():
    value = runtime()
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def slow():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.03)
            cleaned.set()

    value.cores["qq"].poll = slow
    scheduler = DesktopScheduler(value, tick=0.005)
    task = asyncio.create_task(scheduler._run())
    await asyncio.wait_for(entered.wait(), 2)
    value.store.runs["qq"]["status"] = "paused"
    scheduler.cancel("qq")
    scheduler.cancel("qq")
    assert scheduler.status("qq")["stopping"]
    await until(lambda: cleaned.is_set() and not scheduler.status("qq")["stopping"])
    assert value.cores["wechat"].polls and not value.errors
    await stop(scheduler, task)


@pytest.mark.parametrize("code", ["model_usage_limit", "login_required"])
async def test_model_failure_pauses_one_platform_without_automatic_retry_or_account_change(code):
    value = runtime()
    calls = []

    async def fail():
        calls.append(True)
        raise AssistantError(code, "SYNTHETIC_SECRET_DO_NOT_EXPOSE")

    value.cores["qq"].poll = fail
    scheduler = DesktopScheduler(value, tick=0.005)
    task = asyncio.create_task(scheduler._run())
    await until(lambda: bool(value.errors))
    await until(lambda: value.cores["wechat"].polls >= 3)
    assert calls == [True] and value.errors == [("qq", code)]
    await stop(scheduler, task)


@pytest.mark.parametrize("code", ["network_error", "inbox_timeout", "read_timeout", "platform_read_error"])
@pytest.mark.parametrize("stage", ["poll", "cycle"])
async def test_network_poll_retries_are_bounded_and_explicit_restart_clears_delay(code, stage):
    value = runtime()
    now = [0]
    count = []

    async def fail():
        count.append(True)
        raise AssistantError(code, "Temporary")

    setattr(value.cores["qq"], stage, fail)
    scheduler = DesktopScheduler(value, tick=0.005, clock=lambda: now[0])
    task = asyncio.create_task(scheduler._run())
    for attempt in range(1, 4):
        await until(lambda: len(count) == attempt and scheduler.schedules["qq"].failures == attempt)
        now[0] += 1000
    await until(lambda: bool(value.errors))
    assert value.store.runs["qq"]["status"] == "paused"
    setattr(value.cores["qq"], stage, lambda: asyncio.sleep(0, result=[]))
    value.store.runs["qq"] = {"status": "active", "generation": "qq-2"}
    scheduler.wake("qq")
    await until(lambda: scheduler.schedules["qq"].generation == "qq-2")
    assert scheduler.schedules["qq"].failures == 0
    await stop(scheduler, task)


async def test_handoff_restarts_whole_step_and_never_reuses_old_incoming_events():
    value = runtime()
    entered = asyncio.Event()
    polls = []

    async def poll():
        generation = value.store.runs["qq"]["generation"]
        polls.append(generation)
        if generation == "qq-1":
            entered.set()
            await asyncio.Event().wait()
        return [SimpleNamespace(thread="private", visibility="private", id=generation)]

    value.cores["qq"].poll = poll
    scheduler = DesktopScheduler(value, tick=0.005)
    task = asyncio.create_task(scheduler._run())
    await asyncio.wait_for(entered.wait(), 2)

    def handoff():
        value.tasks.drain(value.tasks.block())
        value.store.runs["qq"]["generation"] = "qq-2"
        value.tasks.release()

    await asyncio.to_thread(handoff)
    await until(lambda: bool(value.cores["qq"].handled))
    assert polls[:2] == ["qq-1", "qq-2"] and set(value.cores["qq"].handled) == {"qq-2"}
    await stop(scheduler, task)


async def test_custom_poll_keeps_same_conversation_order_and_does_not_start_public_cycle():
    value = runtime()
    core = value.cores["qq"]
    events = [
        SimpleNamespace(thread=thread, id=identifier) for thread, identifier in [("a", 1), ("b", 2), ("a", 3)]
    ]
    adapter = SimpleNamespace(runtime_poll=lambda: asyncio.sleep(0, result=events))
    core.platforms = SimpleNamespace(all=lambda: [adapter])
    scheduler = DesktopScheduler(value)
    await value.tasks.run(lambda: scheduler._step("qq"))
    assert core.handled.index(1) < core.handled.index(3)
    assert sorted(core.handled) == [1, 2, 3] and core.cycles == 0


def test_native_thread_opens_once_and_closes_even_with_no_platforms():
    value = runtime()
    value.cores = {}
    scheduler = DesktopScheduler(value, tick=0.005)
    scheduler.open()
    assert scheduler.thread.is_alive()
    with pytest.raises(AssistantError):
        scheduler.open()
    scheduler.close()
    assert not scheduler.thread.is_alive()


async def test_adapter_close_failure_does_not_skip_other_components():
    value = runtime()
    closed = []

    async def first():
        closed.append("qq")

    async def failing():
        closed.append("wechat")
        raise OSError("Synthetic close failure")

    value.cores["qq"].platforms = SimpleNamespace(all=lambda: [SimpleNamespace(close=first)])
    value.cores["wechat"].platforms = SimpleNamespace(all=lambda: [SimpleNamespace(close=failing)])
    scheduler = DesktopScheduler(value, tick=0.005)
    scheduler.stop_requested.set()
    with pytest.raises(OSError):
        await scheduler._run()
    assert sorted(closed) == ["qq", "wechat"]


async def test_failure_to_persist_stop_blocks_all_admission_and_exits_native_thread():
    value = runtime()

    async def failed_poll():
        raise AssistantError("model_auth", "Synthetic failure")

    def unavailable(*args):
        raise OSError("Synthetic unavailable database")

    value.cores["qq"].poll = failed_poll
    value.fail = unavailable
    scheduler = DesktopScheduler(value, tick=0.005)
    scheduler.open()
    await until(lambda: scheduler.failure is not None)
    assert value.tasks.blocked
    scheduler.close()
    assert not scheduler.thread.is_alive() and not value.tasks.attempts


async def test_media_cleanup_failure_is_visible_without_stopping_other_platforms():
    value = runtime()
    calls = []
    def cleanup():
        calls.append(1)
        raise AssistantError('storage_integrity', 'synthetic')
    scheduler = DesktopScheduler(value, tick=0.005, maintenance=cleanup)
    scheduler.open()
    await until(lambda: scheduler.maintenance_error is not None and value.cores['wechat'].polls > 0)
    assert scheduler.maintenance_error == 'storage_integrity' and len(calls) == 1
    assert scheduler.failure is None and not value.tasks.blocked
    scheduler.close()


async def test_shutdown_waits_for_media_cleanup_transaction():
    value = runtime()
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    def cleanup():
        entered.set()
        release.wait(3)
        ended.set()
    scheduler = DesktopScheduler(value, tick=0.005, maintenance=cleanup)
    scheduler.open()
    await until(entered.is_set)
    closing = asyncio.create_task(asyncio.to_thread(scheduler.close))
    await asyncio.sleep(0.02)
    assert not closing.done()
    release.set()
    await closing
    assert ended.is_set() and not scheduler.thread.is_alive()
