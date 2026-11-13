"""Account cutover with real asyncio cancellation and synthetic worker processes."""

import asyncio
import threading

import pytest
from test_isolated_codex import fake_rpc

from companion_core.errors import AssistantError
from qiban.desktop_tasks import TaskBarrier


async def test_idle_maintenance_never_cancels_running_work_and_defers_new_admission():
    gate = TaskBarrier()
    entered, finish = asyncio.Event(), asyncio.Event()
    async def work():
        entered.set()
        await finish.wait()
        return 'done'
    running = asyncio.create_task(gate.run(work))
    await entered.wait()
    with gate.idle_maintenance() as idle:
        assert not idle and not running.done()
    finish.set()
    assert await running == 'done'
    entered.clear()
    with gate.idle_maintenance() as idle:
        assert idle
        queued = asyncio.create_task(gate.run(work))
        await asyncio.sleep(.04)
        assert not entered.is_set()
    assert await queued == 'done'


async def test_idle_maintenance_failure_releases_admission_and_honors_account_block():
    gate = TaskBarrier()
    with pytest.raises(RuntimeError):
        with gate.idle_maintenance() as idle:
            assert idle
            gate.block()
            raise RuntimeError('interrupted cleanup')
    started = asyncio.Event()
    async def work():
        started.set()
    queued = asyncio.create_task(gate.run(work))
    await asyncio.sleep(.04)
    assert not started.is_set() and not gate.maintaining
    gate.release()
    await queued


async def test_cutover_waits_for_cleanup_and_restarts_every_task_on_new_account():
    gate = TaskBarrier()
    account = ["old"]
    entered = [asyncio.Event() for _ in range(3)]
    release_cleanup = asyncio.Event()
    calls, closed = [], []

    async def work(index):
        using = account[0]
        calls.append((index, using))
        if using == "old":
            entered[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                await release_cleanup.wait()
                closed.append(index)
        return using

    tasks = [asyncio.create_task(gate.run(lambda i=i: work(i))) for i in range(3)]
    await asyncio.gather(*(e.wait() for e in entered))
    old = gate.block()
    drain = asyncio.create_task(asyncio.to_thread(gate.drain, old))
    queued = asyncio.create_task(gate.run(lambda: work(3)))
    await asyncio.sleep(0.03)
    assert not drain.done() and len(calls) == 3
    release_cleanup.set()
    await drain
    assert sorted(closed) == [0, 1, 2] and all(not task.done() for task in tasks)
    account[0] = "new"
    gate.release()
    assert await asyncio.gather(*tasks, queued) == ["new"] * 4
    assert calls.count((0, "old")) == 1 and all((i, "new") in calls for i in range(4))


async def test_nested_work_has_one_owner_and_explicit_cancel_never_restarts():
    gate = TaskBarrier()
    started = asyncio.Event()
    calls = []

    async def child():
        calls.append(True)
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(gate.run(lambda: gate.run(child)))
    await started.wait()
    assert len(gate.attempts) == 1
    old = gate.block()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(gate.drain, old)
    gate.release()
    assert calls == [True] and not gate.attempts


async def test_cancel_resistant_task_cannot_enable_new_account_or_return_old_result():
    gate = TaskBarrier()
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def work():
        calls.append(True)
        if len(calls) == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish.wait()
                return "old-result"
        return "new-result"

    task = asyncio.create_task(gate.run(work))
    await started.wait()
    old = gate.block()
    with pytest.raises(AssistantError) as error:
        await asyncio.to_thread(gate.drain, old, 0.03)
    assert error.value.code == "model_tasks_stopping" and gate.blocked
    # A repeated stop does not interrupt the previous attempt's cleanup.
    again = gate.block()
    finish.set()
    await asyncio.to_thread(gate.drain, again)
    gate.release()
    assert await task == "new-result"


async def test_old_guard_remains_invalid_after_admission_reopens():
    gate = TaskBarrier()
    guards, started = [], asyncio.Event()

    async def work():
        import contextvars

        if not guards:
            context = contextvars.copy_context()
            guards.append(lambda: context.run(gate.check))
            started.set()
            await asyncio.Event().wait()
        return True

    task = asyncio.create_task(gate.run(work))
    await started.wait()
    await asyncio.to_thread(gate.drain, gate.block())
    gate.release()
    assert await task
    with pytest.raises(AssistantError):
        guards[0]()


async def test_real_child_process_exits_before_new_account_is_used(tmp_path):
    gate = TaskBarrier()
    started, processes = asyncio.Event(), []
    account = ["old"]

    async def work():
        if account[0] == "new":
            assert all(p.poll() is not None for p in processes)
            return "new"
        rpc = fake_rpc(tmp_path, "import time; time.sleep(60)", seconds=20)
        processes.append(rpc.process)
        started.set()
        try:
            await rpc.receive()
        finally:
            await asyncio.to_thread(rpc.close)

    task = asyncio.create_task(gate.run(work))
    await started.wait()
    await asyncio.to_thread(gate.drain, gate.block())
    assert processes[0].poll() is not None
    account[0] = "new"
    gate.release()
    assert await task == "new"


async def test_application_close_cancels_work_without_takeover():
    gate = TaskBarrier()
    started = threading.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(gate.run(work))
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.to_thread(gate.close)
    with pytest.raises(AssistantError) as error:
        await task
    assert error.value.code == "runtime_closed"


async def test_repeated_caller_cancel_does_not_acknowledge_cleanup_early():
    gate = TaskBarrier()
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    task = asyncio.create_task(gate.run(work))
    await entered.wait()
    old = gate.block()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(AssistantError):
        await asyncio.to_thread(gate.drain, old, .03)
    assert not old[0].stopped.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(gate.drain, old)
