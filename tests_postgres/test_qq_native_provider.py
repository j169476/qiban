import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.runtime import Companion
from companion_qq.module import QQModule
from companion_qq.native_provider import NativeProvider
from tests.test_qq_native_normalization import IDENTITY
from tests_postgres.test_qq_module import OPTIONS, STAMP
from tests_postgres.test_qq_native_receiver import Worker
from tests_postgres.test_qq_native_windows import bound, message
from tests_postgres.test_wechat_role import Host, Model


class NativeWorker(Worker):
    def __init__(self, config):
        super().__init__(0)
        self.closed = False
        self.identity = deepcopy(IDENTITY)
        self.submits = 0

    def open(self):
        assert not self.closed
        return deepcopy(self.identity)

    def close(self):
        self.closed = True

    def request(self, command, **args):
        assert not self.closed
        if command == "identity":
            return deepcopy(self.identity)
        if command == "prepare_text":
            return {"token": "a" * 64, "message_id": "9001"}
        if command == "submit_text":
            self.submits += 1
            return {"message_id": "9001", "reference": "native-seq:5"}
        return super().request(command, **args)

    def arrivals(self):
        self.upper = bound(4)
        self.messages = [message(n, mention=n == 2) for n in range(2, 5)]


@pytest.fixture
async def native(pg_store, tmp_path):
    workers = []
    config = {"account": {"uin": "10001", "uid": "u_self"}, "private_targets": [], "groups": ["10003"]}

    def worker_factory(config):
        worker = NativeWorker(config)
        workers.append(worker)
        return worker

    module = QQModule(pg_store, tmp_path, options={**OPTIONS, "private_targets": []},
                      factory=lambda: NativeProvider(pg_store, config, worker_factory=worker_factory))
    module.probe()
    assert workers[0].closed
    for target in module.reply_targets():
        ReplyAuthorizations(pg_store).update(target.scope, enabled=True, expected_version=None, actor="test")
    model = Model()
    core = Companion(pg_store, model, Host(), [module], clock=lambda: datetime.fromtimestamp(STAMP, timezone.utc),
                     run_key="core-run:qq")
    core.start()
    yield SimpleNamespace(store=pg_store, module=module, core=core, model=model, workers=workers)
    core.stop()
    await module.close()
    assert all(worker.closed for worker in workers)


async def test_runtime_reads_native_batch_and_uses_formal_send_journal(native):
    env = native
    assert await env.module.runtime_poll() == []
    assert env.module.socket is None and env.module.reader is None
    worker = env.workers[-1]
    worker.arrivals()
    assert await env.module.runtime_poll() == []  # Stage, do not schedule early.
    pending = await env.module.runtime_poll()
    assert len(pending) == 1 and len(env.module.decision_batch(pending[0])["members"]) == 3
    result = await env.core.handle(pending[0])
    assert result["action"]["status"] == "succeeded"
    assert worker.submits == 1
    assert env.store.state("qq-native-send:" + result["action"]["id"])["status"] == "confirmed"
    assert await env.module.runtime_poll() == []
    assert await env.module.runtime_poll() == []
    worker.upper = bound(5)
    worker.messages.append({**message(5), "senderUin": "10001", "senderUid": "u_self"})
    assert await env.module.runtime_poll() == []
    assert await env.module.runtime_poll() == []
    assert worker.submits == 1


async def test_stop_closes_owned_worker_and_restart_publishes_saved_pages(native):
    env = native
    await env.module.runtime_poll()
    worker = env.workers[-1]
    worker.arrivals()
    await env.module.runtime_poll()
    cursor_key = env.module.provider.receiver.key("group", "10003")
    assert env.store.state(cursor_key)["phase"] == "ready"
    env.core.stop()
    assert worker.closed and not env.module.ready
    await env.module.close()
    env.core.start()
    pending = await env.module.runtime_poll()
    assert len(pending) == 1 and len(env.module.decision_batch(pending[0])["members"]) == 3
    assert not env.workers[-1].calls  # Identity was checked; no head/history request.
    assert env.store.state(cursor_key)["phase"] == "idle"


async def test_read_only_and_unauthorized_scopes_do_not_read_messages(native):
    env = native
    env.core.stop()
    await env.module.close()
    env.core.start(read_only=True)
    assert await env.module.runtime_poll() == []
    assert not env.workers[-1].calls
    env.core.stop()
    await env.module.close()
    auth = ReplyAuthorizations(env.store)
    scope = ReplyScope("qq", "10001", "group", "10003")
    auth.update(scope, enabled=False, expected_version=auth.version(scope), actor="test")
    env.core.start()
    assert await env.module.runtime_poll() == []
    assert not env.workers[-1].calls


async def test_changed_native_identity_closes_worker_before_history(native):
    env = native
    await env.module.runtime_poll()
    worker = env.workers[-1]
    before = len(worker.calls)
    worker.identity["uid"] = "u_other"
    with pytest.raises(AssistantError) as error:
        await env.module.runtime_poll()
    assert error.value.code == "qq_account_mismatch"
    assert worker.closed and len(worker.calls) == before and not env.module.ready


async def test_stop_during_native_read_interrupts_without_advancing_cursor(native):
    from threading import Event

    env = native
    await env.module.runtime_poll()
    worker = env.workers[-1]
    worker.arrivals()
    key = env.module.provider.receiver.key("group", "10003")
    entered, released = Event(), Event()
    original, original_close = worker.request, worker.close

    def blocking(command, **args):
        if command == "group_open":
            entered.set()
            assert released.wait(5)
            raise AssistantError("qq_native_disconnected", "test interruption")
        return original(command, **args)

    def close():
        original_close()
        released.set()

    worker.request, worker.close = blocking, close
    task = asyncio.create_task(env.module.runtime_poll())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        env.core.stop()
        assert worker.closed
        with pytest.raises(AssistantError):
            await asyncio.wait_for(task, 5)
        assert env.store.state(key)["phase"] == "collecting"
        assert env.store.state(key)["lower"] == bound(1)
        assert env.module.native_inbox.count() == 0
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_stop_interrupts_submit_before_waiting_for_ledger_lock(native):
    from threading import Event

    env = native
    await env.module.runtime_poll()
    worker = env.workers[-1]
    worker.arrivals()
    await env.module.runtime_poll()
    pending = await env.module.runtime_poll()
    entered, released = Event(), Event()
    original, original_close = worker.request, worker.close

    def blocking(command, **args):
        if command == "submit_text":
            worker.submits += 1
            entered.set()
            assert released.wait(10)
            raise AssistantError("qq_native_disconnected", "test interrupted submit")
        return original(command, **args)

    def close():
        original_close()
        released.set()

    worker.request, worker.close = blocking, close
    sending = asyncio.create_task(env.core.handle(pending[0]))
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.create_task(asyncio.to_thread(env.core.stop, "account_switch"))
        assert await asyncio.to_thread(released.wait, 2)  # Stop did not wait for send timeout/ledger lock.
        await asyncio.wait_for(stopping, 5)
        await asyncio.gather(sending, return_exceptions=True)
        assert worker.closed and worker.submits == 1
        with env.store.db() as db:
            row = db.execute("SELECT id,status FROM core_actions WHERE platform='qq'").fetchone()
        assert row["status"] == "uncertain"
        assert env.store.state("qq-native-send:" + row["id"])["status"] == "attempted"
        await env.module.close()
        env.core.start()
        assert await env.module.runtime_poll() == []
        assert env.workers[-1].submits == 0
    finally:
        released.set()
        await asyncio.gather(sending, *([stopping] if stopping else []), return_exceptions=True)


async def test_old_stop_generation_cannot_interrupt_replacement_worker(native):
    env = native
    await env.module.runtime_poll()
    old = env.core.guard()["generation"]
    env.core.stop()
    await env.module.close()
    env.core.start()
    await env.module.runtime_poll()
    worker = env.workers[-1]
    env.module.runtime_interrupt(old)
    assert not worker.closed and env.module.ready
