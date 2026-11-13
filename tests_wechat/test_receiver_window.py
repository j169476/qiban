"""Fixed native ranges persist across page drains and process-equivalent restarts."""
import copy

import pytest

from companion_core.errors import AssistantError
from companion_wechat.receiver import DatabaseGateway
from tests_wechat.test_receiver import Factory, Worker, batch, row, snapshot
from tests_wechat.test_receiver import store as store


def window(before, after):
    return {"epoch": before["enrollment"]["epoch"], "sources": {
        key: {**copy.deepcopy(item), "upper": after["checkpoint"]["sources"][key]["anchor"]}
        for key, item in before["checkpoint"]["sources"].items()}}


@pytest.mark.asyncio
async def test_restart_keeps_upper_and_waits_for_all_pages_and_core_cursor(store):
    initial = snapshot()
    first, middle = batch(initial, row(1))
    second, end = batch(middle, row(2))
    frozen = window(initial, end)
    worker = Worker([initial, frozen, {**first, "window_complete": False}])
    gateway = DatabaseGateway(store, {}, capacity=1, fixed_windows=True, transport_factory=Factory(worker))
    await gateway.start()
    await gateway.pump_once()
    incoming, cursor = await gateway.connector.receive({})
    assert len(incoming) == 1 and not gateway.batch_ready(cursor)
    assert gateway._saved["window"] == frozen
    await gateway.close()

    # New source heads could be later. Resumption must not freeze again.
    worker = Worker([middle, {**second, "window_complete": True}, {**second, "window_complete": True}])
    resumed = DatabaseGateway(store, {}, capacity=1, fixed_windows=True, transport_factory=Factory(worker))
    await resumed.start()
    assert (await resumed.pump_once())["state"] == "backpressure"  # First page not acknowledged yet.
    assert resumed._saved["checkpoint"] == middle["checkpoint"]
    await resumed.connector.receive(cursor)
    await resumed.pump_once()
    assert not resumed.batch_ready(cursor)  # Source complete, core has not received the final page.
    incoming, end_cursor = await resumed.connector.receive(cursor)
    assert len(incoming) == 1 and resumed.batch_ready(end_cursor)
    assert all(c[1]["window"] == frozen for c in worker.calls if c[0] == "read_window")
    calls = len(worker.calls)
    await resumed.pump_once()
    assert len(worker.calls) == calls  # Do not absorb later arrivals while the last page is unacknowledged.
    await resumed.close()


@pytest.mark.asyncio
async def test_bounds_survive_failure_before_first_page(store):
    from companion_wechat.receiver_transport import WorkerError

    initial = snapshot()
    data, end = batch(initial, row(1))
    frozen = window(initial, end)
    gateway = DatabaseGateway(store, {}, fixed_windows=True,
        transport_factory=Factory(Worker([initial, frozen, WorkerError("worker_disconnected")])))
    await gateway.start()
    assert (await gateway.pump_once())["state"] == "retrying"
    assert gateway.inbox.checkpoint(gateway.source_key)["window"] == frozen
    await gateway.close()
    worker = Worker([initial, {**data, "window_complete": True}])
    resumed = DatabaseGateway(store, {}, fixed_windows=True, transport_factory=Factory(worker))
    await resumed.start()
    await resumed.pump_once()
    assert [c[0] for c in worker.calls] == ["open", "read_window"]
    assert len((await resumed.connector.receive({}))[0]) == 1
    await resumed.close()


@pytest.mark.asyncio
async def test_false_completion_never_publishes_page_or_advances_cursor(store):
    initial = snapshot()
    data, middle = batch(initial, row(1))
    _, end = batch(middle, row(2))
    gateway = DatabaseGateway(store, {}, fixed_windows=True,
        transport_factory=Factory(Worker([initial, window(initial, end), {**data, "window_complete": True}])))
    await gateway.start()
    with pytest.raises(AssistantError) as error:
        await gateway.pump_once()
    assert error.value.code == "wechat_source_invalid"
    assert gateway._saved["checkpoint"] == initial["checkpoint"]
    assert (await gateway.connector.receive({}))[0] == []
    await gateway.close()


@pytest.mark.asyncio
async def test_later_messages_use_new_window_only_after_previous_delivery_ack(store):
    initial = snapshot()
    first, middle = batch(initial, row(1))
    second, end = batch(middle, row(2))
    worker = Worker([initial, window(initial, middle), {**first, "window_complete": True},
                     window(middle, end), {**second, "window_complete": True}])
    gateway = DatabaseGateway(store, {}, fixed_windows=True, transport_factory=Factory(worker))
    await gateway.start()
    await gateway.pump_once()
    first_rows, cursor = await gateway.connector.receive({})
    assert gateway.batch_ready(cursor)
    await gateway.connector.receive(cursor)
    await gateway.pump_once()
    assert not gateway.batch_ready(cursor)
    second_rows, cursor = await gateway.connector.receive(cursor)
    assert gateway.batch_ready(cursor)
    assert first_rows[0].id != second_rows[0].id
    assert worker.calls[-1][1]["window"] == window(middle, end)
    await gateway.close()
