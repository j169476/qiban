"""Real encrypted PostgreSQL/core, entirely synthetic source and native endpoints."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_policy import ReplyPolicies
from companion_core.runtime import Companion
from companion_wechat.gateway import PermitAuthority
from companion_wechat.module import ContinuousAdapter, WeChatModule
from companion_wechat.native_sender import NativeProof, NativeSenderGateway
from companion_wechat.receiver import DatabaseGateway
from companion_wechat.receiver_transport import WorkerError
from companion_wechat.session import RuntimeSession
from tests_postgres.test_wechat_role import Host, Model, enqueue
from tests_wechat.test_receiver import STAMP, Factory, Worker, batch, row, snapshot


class Driver:
    tainted = False

    def __init__(self):
        self.sent = []
        self.closed = self.revoked = False
        self.before = lambda: None

    async def prepare(self, request):
        self.before()
        return self.inspect(request)

    def inspect(self, request):
        return NativeProof(request.account_identity, request.target_ref, "synthetic-native-session",
                           request.fingerprint, 1, True)

    def submit_once(self, request, proof):
        from companion_wechat.live_sender import DriverOutcome
        assert not self.revoked
        self.sent.append(request)
        return DriverOutcome("confirmed", True)

    def revoke(self):
        self.revoked = True

    async def close(self):
        self.closed = True


async def compose(store, *, family=False, account="synthetic-continuous", clock=None, previous=None, authorized=True):
    worker = Worker([previous or snapshot()])
    receiver = DatabaseGateway(store, {}, account=account, transport_factory=Factory(worker))
    await receiver.start()
    if authorized:
        permissions = ReplyAuthorizations(store)
        for alias in (("owner_main", "family") if family else ("owner_main",)):
            binding = receiver.bindings.get(alias)
            scope = ReplyScope("wechat", receiver.bindings.identity, binding.kind, binding.target_ref)
            permissions.update(scope, enabled=True, expected_version=permissions.record(scope)["version"], actor="test-admin")
    now = datetime.fromtimestamp(STAMP, timezone.utc)
    session = RuntimeSession(store, account, family=family, clock=clock or (lambda: now))
    driver, permits = Driver(), PermitAuthority(b"synthetic-authority-key-00000000000", clock=lambda: 0)
    sender = NativeSenderGateway(store, receiver, permits, driver, session, timeout=25)
    adapter = ContinuousAdapter(receiver, sender, permits, session)
    core = Companion(store, Model(), Host(), [adapter], clock=clock or (lambda: now), run_key="core-run:wechat")
    return SimpleNamespace(store=store, receiver=receiver, worker=worker, session=session, driver=driver,
                           sender=sender, adapter=adapter, core=core, model=core.decisions.model)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", [False, True])
async def test_continuous_ordinary_messages_identical_bubbles_and_group_optin(pg_store, family):
    env = await compose(pg_store, family=family)
    try:
        env.core.start()
        enqueue(env, row(1), row(2), row(1, group=True) | {"trigger": "mention"}, row(2, group=True, sender="synthetic-other"))
        incoming = await env.adapter.collect_once()
        handled = []
        while incoming:
            for event in incoming:
                result = await env.core.handle(event)
                assert result["action"]["status"] == "succeeded", result
                handled.append(event)
            incoming = env.adapter.admitted_pending()
        assert len(handled) == (4 if family else 2)
        assert len({event.id for event in handled}) == len(handled)
        assert len(env.driver.sent) == len(handled)
        enqueue(env, row(3, text="next ordinary message"))
        subsequent = await env.adapter.collect_once()
        assert len(subsequent) == 1
        assert (await env.core.handle(subsequent[0]))["action"]["status"] == "succeeded"
        assert len(env.driver.sent) == len(handled) + 1
        assert env.adapter.admitted_pending() == []
        with pg_store.db() as db:
            assert all(r[0].startswith("enc1:") for r in db.execute("SELECT body FROM core_events"))
            assert all(r[0].startswith("enc1:") for r in db.execute("SELECT body FROM core_actions"))
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('group', [False, True])
async def test_continuous_slow_guards_do_not_expire_unsent_permission(pg_store, monkeypatch, group):
    env = await compose(pg_store, family=group)
    now, issued = [0.0], []
    original_check, original_issue = env.session.check_request, env.sender.permits.issue

    def slow_check(*args, **kwargs):
        now[0] += 1.0
        return original_check(*args, **kwargs)

    def issue(*args, **kwargs):
        issued.append(now[0])
        return original_issue(*args, **kwargs)

    env.sender.permits.clock = lambda: now[0]
    monkeypatch.setattr(env.session, 'check_request', slow_check)
    monkeypatch.setattr(env.sender.permits, 'issue', issue)
    try:
        env.core.start()
        first = row(1, group=group)
        if group:
            first['trigger'] = 'mention'
        enqueue(env, first, row(2, group=group))
        incoming = await env.adapter.collect_once()
        while incoming:
            for event in incoming:
                result = await env.core.handle(event)
                assert result['action']['status'] == 'succeeded', result
            incoming = env.adapter.admitted_pending()
        assert len(env.driver.sent) == 2 and len(issued) == 2
        assert issued[0] > 2 and issued[1] - issued[0] > 2
        assert env.sender.permits.lifetime_seconds == 2.0
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_restart_resumes_unsent_admission_and_cursor_without_resending_completed(pg_store):
    env = await compose(pg_store)
    env.core.start()
    enqueue(env, row(1), row(2))
    incoming = await env.adapter.collect_once()
    assert (await env.core.handle(incoming[0]))["action"]["status"] == "succeeded"
    checkpoint, epoch = env.receiver._saved, env.receiver.inbox.epoch
    env.core.stop("synthetic-restart")
    await env.adapter.close()
    resumed = await compose(pg_store, previous=checkpoint)
    try:
        resumed.core.start()
        enqueue(resumed)
        remaining = await resumed.adapter.collect_once()
        assert [e.id for e in remaining] == [incoming[1].id]
        assert resumed.receiver.inbox.epoch == epoch
        assert (await resumed.core.handle(remaining[0]))["action"]["status"] == "succeeded"
        assert len(resumed.driver.sent) == 1
        with pytest.raises(AssistantError) as rejected:
            await resumed.core.handle(incoming[0])
        assert rejected.value.code == "wechat_role_not_admitted"
        assert len(resumed.driver.sent) == 1
    finally:
        resumed.core.stop()
        await resumed.adapter.close()


@pytest.mark.asyncio
async def test_no_historical_or_nonowner_message_reaches_model(pg_store):
    env = await compose(pg_store)
    try:
        env.core.start()
        old = row(1)
        old["occurred_at"] = datetime.fromtimestamp(STAMP - 60, timezone.utc).isoformat()
        enqueue(env, old, row(1, group=True), row(2, group=True, sender="synthetic-other"))
        assert await env.adapter.collect_once() == []
        assert env.model.calls == env.driver.sent == []
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_group_batch_order_caps_rewake_and_separate_private_quota(pg_store):
    env = await compose(pg_store, family=True)
    ReplyPolicies(pg_store).update("wechat", env.receiver.bindings.identity,
        {"group_session_replies": 2, "private_daily_replies": 1}, None, "test")
    try:
        env.core.start()
        enqueue(env, row(1, group=True), row(2, group=True) | {"trigger": "mention"},
                row(3, group=True, sender="synthetic-other"), row(4, group=True),
                row(5, group=True) | {"trigger": "mention"}, row(6, group=True))
        pending = await env.adapter.collect_once()
        seen = []
        while pending:
            assert len(pending) == 1
            seen.append(pending[0].native_id)
            assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
            pending = env.adapter.admitted_pending()
        assert len(seen) == 4 and len(set(seen)) == 4
        enqueue(env, row(1))
        private = await env.adapter.collect_once()
        assert (await env.core.handle(private[0]))["action"]["status"] == "succeeded"
        enqueue(env, row(2))
        assert await env.adapter.collect_once() == []
        assert len(env.driver.sent) == 5
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_group_topic_end_closes_window_without_native_send(pg_store):
    env = await compose(pg_store, family=True)
    async def end(*_args, **_kwargs):
        return {"note": "topic ended", "action": {"capability": "conversation.end", "arguments": {}},
                "memories": [], "identity_proposals": [], "relationship": None}
    env.model.generate = end
    try:
        env.core.start()
        enqueue(env, row(1, group=True) | {"trigger": "mention"}, row(2, group=True))
        first = await env.adapter.collect_once()
        result = await env.core.handle(first[0])
        assert result["action"]["receipt"]["participation_ended"]
        assert env.adapter.admitted_pending() == [] and not env.driver.sent
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_native_uncertainty_stops_new_actions(pg_store):
    env = await compose(pg_store)
    try:
        env.core.start()
        env.driver.tainted = True
        with pytest.raises(AssistantError) as error:
            env.core.guard()
        assert error.value.code == "wechat_native_unconfirmed"
        assert env.driver.sent == []
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_lazy_module_start_stop_cleanup_and_restart(pg_store, tmp_path):
    env = await compose(pg_store)
    module = WeChatModule(pg_store, tmp_path, "synthetic-install", factory=lambda: env.adapter)
    module.account = env.receiver.account
    # Replace the fixture's direct core binding with the actual service wrapper.
    env.adapter.core = None
    env.session.core = None
    core = Companion(pg_store, Model(), Host(), [module], clock=env.core.clock, run_key="core-run:wechat")
    assert module.delegate is None
    core.start()
    assert module.runtime_status()["state"] == "running"
    core.stop()
    assert module.runtime_status()["state"] == "stopping"
    assert env.driver.revoked
    with pytest.raises(AssistantError):
        core.start()
    await module.idle()
    assert env.worker.closed and env.driver.closed and not env.session._lease.is_locked
    assert module.runtime_status()["state"] == "paused"
    resumed = await compose(pg_store, previous=env.receiver._saved)
    resumed.adapter.core = resumed.session.core = None
    module.factory = lambda: resumed.adapter
    core.start()
    assert module.runtime_status()["state"] == "running"
    core.stop()
    await module.close()


@pytest.mark.asyncio
async def test_disconnected_source_defers_admitted_reply_until_reconnect(pg_store):
    env = await compose(pg_store)
    try:
        env.core.start()
        enqueue(env, row(1))
        assert len(await env.adapter.collect_once()) == 1
        env.worker.responses.append(WorkerError("client_not_running"))
        assert await env.adapter.collect_once() == []
        assert env.model.calls == [] and len(env.session.admitted_pending()) == 1
        resumed_worker = Worker([env.receiver._saved])
        response, _ = batch(env.receiver._saved)
        resumed_worker.responses.append(response)
        env.receiver.transport_factory = Factory(resumed_worker)
        env.receiver._next_retry = 0
        pending = await env.adapter.collect_once()
        assert len(pending) == 1
        assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_external_watchdog_pause_releases_worker_and_collector_lock(pg_store, tmp_path):
    env = await compose(pg_store)
    module = WeChatModule(pg_store, tmp_path, "synthetic-install", factory=lambda: env.adapter)
    module.account = env.receiver.account
    env.adapter.core = env.session.core = None
    core = Companion(pg_store, Model(), Host(), [module], clock=env.core.clock, run_key="core-run:wechat")
    run = core.start()
    pg_store.set_state(core.run_key, {**run, "status": "paused", "reason": "heartbeat_lost"})
    assert not module.stopping
    await module.idle()
    assert env.driver.revoked and env.driver.closed and env.worker.closed
    assert not env.session._lease.is_locked and module.delegate is None


@pytest.mark.asyncio
async def test_service_scheduler_uses_admitted_group_events_and_closes_module(pg_store, tmp_path, monkeypatch):
    from companion_service import platform_runs as implementation

    env = await compose(pg_store, family=True)
    module = WeChatModule(pg_store, tmp_path, "synthetic-install", factory=lambda: env.adapter)
    module.account = env.receiver.account
    env.adapter.core = env.session.core = None
    service = SimpleNamespace(store=pg_store, host=Host(), shutdown=asyncio.Event(), storage_unavailable=False,
                              maintenance_until=0, supervisor=None)
    runs = implementation.PlatformRuns(service, Model(), [module])
    core = runs.cores["wechat"]
    core.clock = env.core.clock
    core.host.thread_id = "synthetic-role-controller"
    core.start()
    assert (await asyncio.to_thread(core.guard))["status"] == "active"
    enqueue(env, row(1, group=True) | {"trigger": "mention"}, row(2, group=True, sender="synthetic-other"))

    async def forbidden():
        pytest.fail("continuous scheduler must not fetch unadmitted historical pending events")

    async def tick(_seconds):
        await asyncio.sleep(0)

    original = env.driver.submit_once

    def finish(request, proof):
        service.shutdown.set()
        return original(request, proof)

    env.driver.submit_once = finish
    core.poll = core.cycle = forbidden
    monkeypatch.setattr(implementation, "asyncio", SimpleNamespace(sleep=tick, wait_for=asyncio.wait_for,
                                                                  gather=asyncio.gather, to_thread=asyncio.to_thread))
    try:
        await asyncio.wait_for(runs.work("wechat"), 5)
        # Stop after the first send: the next group decision stays queued.
        assert len(env.driver.sent) == 1 and env.driver.sent[0].conversation == "family"
        assert runs.status("wechat")["connectors"][0]["session"]["family_enabled"]
    finally:
        core.stop()
        await runs.close()
    assert env.driver.closed and env.worker.closed
