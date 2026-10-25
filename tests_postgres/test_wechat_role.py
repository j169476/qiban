"""Real encrypted PG/core/receiver path; source, model and native driver are synthetic."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from companion_core import events
from companion_core.runtime import Companion
from companion_wechat.gateway import PermitAuthority
from companion_wechat.live_sender import DriverOutcome, LiveSenderGateway, SendProof
from companion_wechat.receiver import DatabaseGateway
from companion_wechat.role import RoleAdapter, RoleSession
from tests_wechat.test_receiver import STAMP, Factory, Worker, batch, row, snapshot

ACCOUNT = "synthetic-pg-role"


class Host:
    def identity(self):
        return "synthetic-role-controller"

    def require_active(self, _thread_id=None):
        pass


class Model:
    def __init__(self):
        self.calls = []

    async def generate(self, _instructions, context, _schema, _images=(), **_kwargs):
        self.calls.append(context)
        return {"note": "", "action": {"capability": "message.reply", "arguments": {"text": "synthetic reply"}},
                "memories": [], "identity_proposals": [], "relationship": None}


class Driver:
    def __init__(self):
        self.sent, self.closed = [], False
        self.outcome = DriverOutcome("confirmed", client_recorded=True)
        self.before = lambda: None

    async def prepare(self, request):
        self.before()
        return self.inspect(request)

    def inspect(self, request):
        return SendProof(request.account_identity, request.target_ref, "synthetic-native-login", "synthetic-target",
                         "synthetic-empty-draft", ready=True, draft_empty=True, manual_takeover=False)

    def submit_once(self, request, _proof):
        self.sent.append(request)
        return self.outcome

    async def close(self):
        self.closed = True


@pytest.fixture
async def env(pg_store):
    worker = Worker([snapshot()])
    receiver = DatabaseGateway(pg_store, {}, account=ACCOUNT, transport_factory=Factory(worker))
    await receiver.start()
    now = datetime.fromtimestamp(STAMP, timezone.utc)
    session = RoleSession(pg_store, ACCOUNT, clock=lambda: now, nonce_factory=lambda: "B" * 24)
    permits = PermitAuthority(b"synthetic-permit-key-for-tests!!!" * 2, clock=lambda: 0.0)
    driver = Driver()
    sender = LiveSenderGateway(pg_store, receiver, permits, driver, session)
    adapter = RoleAdapter(receiver, sender, permits, session)
    model = Model()
    core = Companion(pg_store, model, Host(), [adapter], clock=lambda: now, run_key="core-run:wechat")
    yield SimpleNamespace(store=pg_store, receiver=receiver, worker=worker, session=session, driver=driver,
                          sender=sender, adapter=adapter, core=core, model=model)
    if pg_store.state(core.run_key, {}).get("status") == "active":
        core.stop("synthetic-test-complete")
    await adapter.close()


def enqueue(env, *rows):
    response, _after = batch(env.receiver._saved, *rows)
    env.worker.responses.append(response)


@pytest.mark.asyncio
async def test_core_handles_two_admitted_targets_and_records_encrypted_replies_once(env):
    original = env.receiver.inbox.checkpoint(env.receiver.source_key)
    epoch = env.receiver.inbox.epoch
    bindings = env.store.state(env.receiver.bindings.key)
    env.core.start()
    env.core.guard()
    assert env.receiver.inbox.checkpoint(env.receiver.source_key) == original
    assert env.receiver.inbox.epoch == epoch and env.store.state(env.receiver.bindings.key) == bindings
    token = env.session.nonce
    enqueue(env, row(1, text=token + " private"), row(1, group=True, text=token + " group"),
            row(2, group=True, sender="synthetic-other", text=token + " not owner"),
            row(2, text="ordinary private message"))
    admitted = await env.adapter.collect_once()
    assert {event.conversation for event in admitted} == {"owner_main", "family"}
    assert next(event for event in admitted if event.visibility == "group").metadata["wechat"]["trigger"] == "none"
    for event in admitted:
        result = await env.core.handle(event)
        assert result["action"]["status"] == "succeeded", result["action"]
    assert len(env.model.calls) == len(env.driver.sent) == 2
    assert env.adapter.admitted_pending() == []
    assert env.session.public_status()["completed"] == {"owner_main": 1, "family": 1}
    for event in admitted:
        assert (await env.core.handle(event))["duplicate"] is True
    assert len(env.model.calls) == len(env.driver.sent) == 2
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM core_actions WHERE status='succeeded'").fetchone()[0] == 2
        assert all(record[0].startswith("enc1:") for record in db.execute("SELECT body FROM core_events"))
        assert db.execute("SELECT COUNT(*) FROM content_records WHERE kind='conversation_turns'").fetchone()[0] == 4


@pytest.mark.asyncio
async def test_existing_core_event_is_never_admitted_even_if_nonce_were_reused(env):
    old = row(1, text="XLD-ROLE-" + "B" * 24 + " old synthetic message")
    observation = env.receiver._observations([old])[0]
    env.receiver.inbox.publish(observation)
    found, _cursor = await env.receiver.connector.receive({})
    events.ingest(env.store, found[0])
    env.core.start()
    enqueue(env)
    assert await env.adapter.collect_once() == []
    assert env.model.calls == [] and env.driver.sent == []
    assert env.session.public_status()["admitted"] == {"owner_main": 0, "family": 0}


@pytest.mark.asyncio
async def test_custom_workflow_never_queries_unadmitted_pending_inbox(env, monkeypatch):
    env.core.start()
    enqueue(env, row(1, text="ordinary"), row(1, group=True, text=env.session.nonce + " approved"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("role workflow must not query historical/pending inbox indiscriminately")

    monkeypatch.setattr(events, "pending", forbidden)
    await env.core.cycle()
    assert len(env.model.calls) == len(env.driver.sent) == 1
    assert env.driver.sent[0].conversation == "family"


@pytest.mark.asyncio
async def test_unknown_submission_is_not_resent_by_core_replay(env):
    env.core.start()
    enqueue(env, row(1, text=env.session.nonce + " approved"))
    event = (await env.adapter.collect_once())[0]
    env.driver.outcome = DriverOutcome("uncertain")
    result = await env.core.handle(event)
    assert result["action"]["status"] == "uncertain"
    assert (await env.core.handle(event))["duplicate"] is True
    assert len(env.driver.sent) == len(env.model.calls) == 1
    assert env.session.public_status()["reserved"]["owner_main"] == 1
    assert env.session.public_status()["completed"]["owner_main"] == 0


@pytest.mark.asyncio
async def test_source_event_cursor_and_admission_rollback_together(env, monkeypatch):
    env.core.start()
    enqueue(env, row(1, text=env.session.nonce + " approved"))
    original = env.store.set_state

    def fail_cursor(key, value, db=None):
        if key.startswith("cursor-"):
            raise RuntimeError("synthetic consumer failure")
        return original(key, value, db)

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "set_state", fail_cursor)
        with pytest.raises(RuntimeError):
            await env.adapter.collect_once()
    assert env.session.public_status()["admitted"] == {"owner_main": 0, "family": 0}
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 0
    enqueue(env)
    recovered = await env.adapter.collect_once()
    assert len(recovered) == 1
    assert env.session.public_status()["admitted"]["owner_main"] == 1
