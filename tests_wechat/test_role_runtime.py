import asyncio
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_wechat.role_runtime import RoleTestRunner, run_role_test


class Store:
    def __init__(self):
        self.active = False

    def state(self, key, default):
        return {"status": "active" if self.active else "paused"}


class Adapter:
    def __init__(self, store, events=()):
        self.store, self.events, self.closed = store, list(events), False
        self.session = SimpleNamespace(public_status=lambda: {
            "nonce": "XLD-ROLE-SYNTHETIC", "status": "revoked" if self.closed else "active"},
            remaining_seconds=lambda: 180.0)
        self.sender = SimpleNamespace(preflight=lambda: None)
        self.polls = 0

    async def collect_once(self):
        self.polls += 1

    def admitted_pending(self):
        return self.events

    async def close(self):
        self.closed = True


class Core:
    run_key = "core-run:wechat"

    def __init__(self, store, adapter):
        self.store, self.platforms = store, SimpleNamespace(all=lambda: [adapter])
        self.starts, self.stops, self.handled = 0, [], []
        self.expired = False

    def start(self, *, read_only):
        assert read_only is False
        self.starts += 1
        self.store.active = True
        return {"generation": "synthetic-generation"}

    def guard(self, generation):
        assert generation == "synthetic-generation"
        if self.expired:
            raise AssistantError("wechat_role_expired", "synthetic expiry")

    async def handle(self, event):
        self.handled.append(event.conversation)
        return {"action": {"status": "succeeded"}}

    def stop(self, reason, *, generation):
        self.stops.append((reason, generation))
        self.store.active = False


def setup(events=()):
    store = Store()
    adapter = Adapter(store, [SimpleNamespace(conversation=alias) for alias in events])
    core = Core(store, adapter)
    return core, adapter, RoleTestRunner(core, adapter)


@pytest.mark.asyncio
async def test_two_targets_uses_existing_core_handle_then_stops_and_closes():
    core, adapter, runner = setup(("owner_main", "family"))
    emitted = []
    result = await runner.run(emit=emitted.append)
    assert core.handled == ["owner_main", "family"] and core.starts == 1
    assert result["status"] == "complete" and result["succeeded"] == 2
    assert result["role_stopped"] and adapter.closed and not core.store.active
    assert core.stops == [("wechat_test_finished", "synthetic-generation")]
    assert emitted[-1]["test"]["status"] == "revoked"


@pytest.mark.asyncio
async def test_missing_driver_never_starts_or_calls_model_and_closes_source():
    core, adapter, runner = setup()

    def unavailable():
        raise AssistantError("wechat_live_driver_missing", "synthetic")

    adapter.sender.preflight = unavailable
    with pytest.raises(AssistantError) as error:
        await runner.run()
    assert error.value.code == "wechat_live_driver_missing"
    assert core.starts == 0 and core.handled == [] and adapter.closed and not core.stops


@pytest.mark.asyncio
async def test_does_not_replace_another_active_run():
    core, adapter, runner = setup()
    core.store.active = True
    with pytest.raises(AssistantError) as error:
        await runner.run()
    assert error.value.code == "wechat_role_already_running"
    assert core.store.active and not core.stops and core.starts == 0 and adapter.closed


@pytest.mark.asyncio
async def test_expiry_reports_after_cleanup_without_waiting_real_time():
    core, adapter, runner = setup()
    now = [0.0]

    async def advance(seconds):
        now[0] += seconds
        core.expired = True

    runner.clock, runner.sleep = lambda: now[0], advance
    result = await runner.run()
    assert result["status"] == "deadline" and result["role_stopped"]
    assert adapter.closed and core.stops and core.handled == []


@pytest.mark.asyncio
async def test_cancellation_stops_owned_generation_and_releases_source():
    core, adapter, runner = setup()

    async def cancel(_seconds):
        raise asyncio.CancelledError()

    runner.sleep = cancel
    output = []
    with pytest.raises(asyncio.CancelledError):
        await runner.run(emit=output.append)
    assert core.stops and adapter.closed
    assert output[-1]["status"] == "interrupted" and output[-1]["role_stopped"]


@pytest.mark.asyncio
async def test_untrusted_model_error_is_sanitized_and_does_not_send_to_next_target():
    core, adapter, runner = setup(("owner_main", "family"))

    async def fail(event):
        raise RuntimeError("SYNTHETIC_PRIVATE_DETAILS")

    core.handle = fail
    with pytest.raises(AssistantError) as error:
        await runner.run()
    assert error.value.code == "wechat_role_failed" and "SYNTHETIC_PRIVATE" not in str(error.value)
    assert core.stops and adapter.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["uncertain", "cancelled", "failed", None])
async def test_once_per_target_even_without_confirmed_submission(outcome):
    core, adapter, runner = setup(("owner_main", "owner_main", "family", "family"))

    async def handle(event):
        core.handled.append(event.conversation)
        return {"action": {"status": outcome} if outcome else None}

    core.handle = handle
    result = await runner.run()
    assert core.handled == ["owner_main", "family"]
    assert result[outcome or "no_action"] == 2 and result["handled"] == 2


@pytest.mark.asyncio
async def test_production_composition_rejects_missing_driver_before_opening_source():
    with pytest.raises(AssistantError) as error:
        await run_role_test(None, None, None, None)
    assert error.value.code == "wechat_live_driver_missing"


@pytest.mark.asyncio
async def test_production_composition_rejects_plaintext_store_before_opening_source():
    with pytest.raises(AssistantError) as error:
        await run_role_test(SimpleNamespace(store=Store()), None, None, object())
    assert error.value.code == "wechat_send_store_unencrypted"


@pytest.mark.asyncio
async def test_hanging_model_is_cancelled_at_bounded_deadline():
    core, adapter, runner = setup(("owner_main",))
    cancelled = []

    async def hang(event):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    core.handle = hang
    adapter.session.remaining_seconds = lambda: 0.03
    result = await runner.run()
    assert result["status"] == "deadline" and result["handled"] == 0 and cancelled == [True]
    assert core.stops and adapter.closed and result["role_stopped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["collect", "handle"])
async def test_consumed_port_cancellation_still_stops_before_next_target(phase):
    core, adapter, runner = setup(("owner_main", "family"))
    entered = asyncio.Event()

    async def port(*args):
        if phase == "handle":
            core.handled.append(args[0].conversation)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Matches the send boundary preserving its durable uncertain result.
            return {"action": {"status": "uncertain"}}

    if phase == "handle":
        core.handle = port
    else:
        adapter.collect_once = port
    task = asyncio.create_task(runner.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert core.handled == (["owner_main"] if phase == "handle" else [])
    assert core.stops and adapter.closed and not core.store.active


@pytest.mark.asyncio
async def test_consumed_send_cancellation_does_not_extend_deadline():
    core, adapter, runner = setup(("owner_main", "family"))

    async def uncertain(event):
        core.handled.append(event.conversation)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"action": {"status": "uncertain"}}

    core.handle = uncertain
    adapter.session.remaining_seconds = lambda: 0.03
    result = await runner.run()
    assert result["status"] == "deadline" and core.handled == ["owner_main"]
    assert core.stops and adapter.closed and result["role_stopped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_driver", [True, False])
async def test_rejected_composition_closes_already_open_source_and_driver(missing_driver):
    closed = []

    async def close_source():
        closed.append("source")

    async def close_driver():
        closed.append("driver")

    receiver = SimpleNamespace(store=Store(), close=close_source)
    driver = None if missing_driver else SimpleNamespace(close=close_driver)
    with pytest.raises(AssistantError) as error:
        await run_role_test(receiver, None, None, driver)
    expected = "wechat_live_driver_missing" if missing_driver else "wechat_send_store_unencrypted"
    assert error.value.code == expected
    assert closed == (["source"] if missing_driver else ["driver", "source"])


@pytest.mark.asyncio
async def test_invalid_runner_options_still_release_injected_resources():
    core, adapter, runner = setup()
    with pytest.raises(AssistantError) as error:
        await runner.run(poll_seconds=0)
    assert error.value.code == "wechat_role_configuration"
    assert adapter.closed and not core.stops and core.starts == 0
