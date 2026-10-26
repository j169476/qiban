"""Synthetic operator frames only; never run Sky or inspect a real account."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.storage import Store
from companion_wechat.connector import WeChatConnector
from companion_wechat.contracts import SendRequest, digest
from companion_wechat.gateway import PermitAuthority
from companion_wechat.live_sender import DriverOutcome, LiveSenderGateway
from companion_wechat.supervised_sender import StagedProof, SupervisedSenderGateway
from tests_wechat.test_live_sender import Driver, Receiver
from tests_wechat.test_live_sender import Session as NativeSession
from tests_wechat.test_receiver_journal import EncryptedTestStore


class Session(NativeSession):
    def check_request(self, request, *, db=None):
        super().check_request(request)


class Operator:
    def __init__(self):
        self.prepares = 0
        self.grants = []
        self.stage_changes = {}
        self.before_claim = lambda: None
        self.after_grant = lambda: None
        self.before_gate = self.after_gate = None
        self.armed = asyncio.Event()
        self.granted = asyncio.Event()
        self.outcome = DriverOutcome("confirmed", True)
        self.authorize = None
        self.closed = False

    async def prepare(self, request):
        self.prepares += 1
        return replace(StagedProof(request.account_identity, request.target_ref, "synthetic-native-login",
                                   "synthetic-observed-draft", digest(request.text), True, True, False),
                       **self.stage_changes)

    async def dispatch_once(self, request, proof, *, authorize):
        self.authorize = authorize
        self.armed.set()
        if self.before_gate is not None:
            await self.before_gate.wait()
        self.before_claim()
        lease = authorize()
        assert isinstance(lease["expires_unix_ms"], int)
        self.grants.append(request.action_id)
        self.granted.set()
        self.after_grant()
        if self.after_gate is not None:
            await self.after_gate.wait()
        return self.outcome

    async def close(self):
        self.closed = True


@pytest.fixture
def env(tmp_path):
    store = EncryptedTestStore(tmp_path / "supervised")
    receiver, operator, now = Receiver(store), Operator(), [100.0]
    permits = PermitAuthority(b"synthetic-supervised-authority-key", clock=lambda: now[0])
    session = Session(store)
    sender = SupervisedSenderGateway(store, receiver, permits, operator, session, clock=lambda: now[0])
    request = SendRequest("synthetic-action", "synthetic-generation", "synthetic-self", "durable-inbox-epoch",
                          "owner_main", 1, "synthetic-owner", "synthetic supervised reply")
    return SimpleNamespace(store=store, receiver=receiver, operator=operator, session=session,
                           permits=permits, sender=sender, request=request, now=now)


async def prepared(env, sender=None):
    ticket = await (sender or env.sender).prepare(env.request)
    return env.permits.issue(ticket, env.request)


def restarted(env):
    store = EncryptedTestStore(env.store.root)
    return SupervisedSenderGateway(store, Receiver(store), env.permits, Operator(), Session(store))


def test_default_missing_operator_and_plaintext_store_fail_closed(env, tmp_path):
    missing = SupervisedSenderGateway(env.store, env.receiver, env.permits, None, env.session)
    assert not missing.available
    with pytest.raises(AssistantError) as caught:
        missing.preflight()
    assert caught.value.code == "wechat_live_driver_missing"
    missing.revoke_generation(None)
    plaintext = SupervisedSenderGateway(Store(tmp_path / "plain"), env.receiver, env.permits,
                                        env.operator, env.session)
    with pytest.raises(AssistantError) as caught:
        plaintext.preflight()
    assert caught.value.code == "wechat_send_store_unencrypted"
    assert not env.operator.prepares


def test_connector_explicitly_accepts_separate_supervised_sender(env):
    connector = WeChatConnector(env.receiver.bindings, env.receiver, env.permits, sender=env.sender)
    assert connector.sender is env.sender and not isinstance(env.sender, LiveSenderGateway)
    assert connector.timeout == 5.0 and env.sender.prepare_timeout == 180
    with pytest.raises(AssistantError):
        WeChatConnector(env.receiver.bindings, env.receiver, env.permits, sender=env.operator)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"observed": False}, {"draft_matches": False}, {"manual_takeover": True},
    {"account_identity": "different-account"}, {"target_ref": "different-target"},
    {"client_epoch": "durable-inbox-epoch"}, {"view_token": ""}, {"text_digest": digest("other text")},
])
async def test_unobserved_or_mismatched_staged_draft_never_arms(env, change):
    env.operator.stage_changes = change
    with pytest.raises(AssistantError):
        await prepared(env)
    assert not env.operator.armed.is_set() and not env.operator.grants


@pytest.mark.asyncio
async def test_manual_prepare_wait_does_not_spend_two_second_permit(env):
    original = env.operator.prepare

    async def delayed(request):
        env.now[0] += 90
        return await original(request)

    env.operator.prepare = delayed
    permit = await prepared(env)
    assert permit.expires_at == env.now[0] + 2
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "succeeded" and result.receipt["client_recorded"]
    assert env.operator.grants == [env.request.action_id]


@pytest.mark.asyncio
async def test_slow_preflight_does_not_extend_final_two_second_ui_grant(env, monkeypatch):
    env.permits = PermitAuthority(b"synthetic-supervised-authority-key", clock=lambda: env.now[0],
                                  lifetime_seconds=20)
    env.sender.permits = env.permits
    monkeypatch.setattr("companion_wechat.supervised_sender.time.time", lambda: 500.0)
    permit = await prepared(env)
    env.operator.before_claim = lambda: env.now.__setitem__(0, env.now[0] + 7)
    seen = []
    original = env.operator.dispatch_once

    async def dispatch(request, proof, *, authorize):
        def capture():
            grant = authorize()
            seen.append(grant)
            return grant
        return await original(request, proof, authorize=capture)

    env.operator.dispatch_once = dispatch
    assert (await env.sender.commit(permit, guard=lambda: None)).status == "succeeded"
    assert seen == [{"expires_unix_ms": 502000}]


@pytest.mark.asyncio
async def test_preparation_lease_cannot_be_extended_or_used_by_native_sender(env):
    env.permits = PermitAuthority(b"synthetic-supervised-authority-key", clock=lambda: env.now[0],
                                  lifetime_seconds=20)
    env.sender.permits = env.permits
    permit = await prepared(env)
    env.operator.before_claim = lambda: env.now.__setitem__(0, env.now[0] + 21)
    assert (await env.sender.commit(permit, guard=lambda: None)).status == "cancelled"
    assert not env.operator.grants
    with pytest.raises(AssistantError):
        PermitAuthority(b"synthetic-supervised-authority-key", lifetime_seconds=21)
    native = LiveSenderGateway(env.store, env.receiver, env.permits, Driver(), env.session)
    with pytest.raises(AssistantError) as error:
        native.preflight()
    assert error.value.code == "wechat_permit_configuration"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "generation", "binding", "close"])
async def test_final_claim_rechecks_after_wait_before_any_grant(env, change):
    permit = await prepared(env)

    def changed():
        if change == "expiry":
            env.now[0] += 3
        elif change == "generation":
            env.sender.revoke_generation(env.request.generation)
        elif change == "binding":
            binding = env.receiver.bindings.get("owner_main")
            env.receiver.bindings.bind(replace(binding, enabled=False), expected_revision=1)
        else:
            env.sender._closed = True

    env.operator.before_claim = changed
    assert (await env.sender.commit(permit, guard=lambda: None)).status == "cancelled"
    assert not env.operator.grants


@pytest.mark.asyncio
async def test_confirmed_without_authorization_is_rejected(env):
    async def bypass(*_args, **_kwargs):
        return DriverOutcome("confirmed", True)

    env.operator.dispatch_once = bypass
    result = await env.sender.commit(await prepared(env), guard=lambda: None)
    assert result.status == "cancelled" and result.error == "wechat_supervised_grant_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DriverOutcome("uncertain"), DriverOutcome("rejected"),
                                     DriverOutcome("confirmed", False), DriverOutcome("rejected", True)])
async def test_after_grant_any_unconfirmed_report_remains_uncertain(env, outcome):
    env.operator.outcome = outcome
    permit = await prepared(env)
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "uncertain" and len(env.operator.grants) == 1
    assert (await restarted(env).commit(permit, guard=lambda: None)) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_cancel_before_grant_is_unsent_after_grant_is_uncertain(env, stage):
    gate = asyncio.Event()
    if stage == "before":
        env.operator.before_gate = gate
    else:
        env.operator.after_gate = gate
    permit = await prepared(env)
    task = asyncio.create_task(env.sender.commit(permit, guard=lambda: None))
    await (env.operator.armed if stage == "before" else env.operator.granted).wait()
    task.cancel()
    result = await task
    assert result.status == ("cancelled" if stage == "before" else "uncertain")
    assert len(env.operator.grants) == (stage == "after")
    with pytest.raises(AssistantError):
        env.operator.authorize()
    assert (await restarted(env).commit(permit, guard=lambda: None)) == result


@pytest.mark.asyncio
async def test_operator_disconnect_after_grant_never_retries(env):
    def disconnected():
        raise RuntimeError("SYNTHETIC_PRIVATE_PIPE_DATA")

    env.operator.after_grant = disconnected
    permit = await prepared(env)
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "uncertain" and "PRIVATE" not in repr(result)
    other = restarted(env)
    assert (await other.commit(await prepared(env, other), guard=lambda: None)) == result
    assert not other.operator.grants and not other.operator.prepares


@pytest.mark.asyncio
async def test_durable_claim_and_grant_failures_prevent_untracked_ui_command(env, monkeypatch):
    original = env.store.set_state

    def failure(key, value, db=None):
        original(key, value, db)
        if key == env.sender._action_key(env.request) and value.get("grant_issued") is True:
            raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(env.store, "set_state", failure)
    result = await env.sender.commit(await prepared(env), guard=lambda: None)
    assert result.status == "cancelled" and not env.operator.grants
    saved = env.store.state(env.sender._action_key(env.request))
    assert saved["grant_issued"] is False


@pytest.mark.asyncio
async def test_permit_expiring_during_grant_transaction_emits_no_grant(env, monkeypatch):
    original = env.store.set_state

    def delayed_commit(key, value, db=None):
        original(key, value, db)
        if key == env.sender._action_key(env.request) and value.get("grant_issued") is True:
            env.now[0] += 3

    monkeypatch.setattr(env.store, "set_state", delayed_commit)
    result = await env.sender.commit(await prepared(env), guard=lambda: None)
    assert result.status == "cancelled" and result.error == "wechat_permit_expired"
    assert not env.operator.grants


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_operator_timeout_never_leaves_a_late_authorization(env, stage):
    if stage == "before":
        env.operator.before_gate = asyncio.Event()
    else:
        env.operator.after_gate = asyncio.Event()
    env.sender.commit_timeout = 0.01
    permit = await prepared(env)
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == ("cancelled" if stage == "before" else "uncertain")
    with pytest.raises(AssistantError):
        env.operator.authorize()
    assert len(env.operator.grants) == (stage == "after")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_process_crash_keeps_intent_and_never_reissues_ui_grant(env, stage):
    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    if stage == "before":
        env.operator.before_claim = crash
    else:
        env.operator.after_grant = crash
    permit = await prepared(env)
    with pytest.raises(Crash):
        await env.sender.commit(permit, guard=lambda: None)
    assert env.store.state(env.sender._action_key(env.request))["status"] == "submitting"
    other = restarted(env)
    assert (await other.commit(permit, guard=lambda: None)).status == "uncertain"
    assert not other.operator.grants


@pytest.mark.asyncio
async def test_two_instances_never_grant_the_same_action_twice(env):
    env.operator.after_gate = asyncio.Event()
    permit = await prepared(env)
    first = asyncio.create_task(env.sender.commit(permit, guard=lambda: None))
    await env.operator.granted.wait()
    other = restarted(env)
    assert (await other.commit(await prepared(env, other), guard=lambda: None)).status == "uncertain"
    assert not other.operator.grants
    env.operator.after_gate.set()
    assert (await first).status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_first", [False, True])
async def test_driver_type_change_cannot_bypass_durable_action_id(env, native_first):
    native = LiveSenderGateway(env.store, env.receiver, env.permits, Driver(), env.session)
    first, second = (native, env.sender) if native_first else (env.sender, native)
    first_permit = env.permits.issue(await first.prepare(env.request), env.request)
    expected = await first.commit(first_permit, guard=lambda: None)
    assert expected.status == "succeeded"
    next_permit = env.permits.issue(await second.prepare(env.request), env.request)
    assert (await second.commit(next_permit, guard=lambda: None)) == expected
    assert len(native.driver.sent) + len(env.operator.grants) == 1
    with pytest.raises(AssistantError):
        await second.prepare(replace(env.request, text="different payload"))


@pytest.mark.asyncio
async def test_confirmed_ticket_replay_after_expiry_is_exact_and_encrypted(env):
    permit = await prepared(env)
    result = await env.sender.commit(permit, guard=lambda: None)
    env.now[0] += 10
    assert await restarted(env).commit(permit, guard=lambda: None) == result
    with env.store.db() as db:
        values = db.execute("SELECT value FROM state WHERE key LIKE 'wechat-live-%'").fetchall()
    assert all(value[0].startswith("enc1:") and env.request.text not in value[0] for value in values)
    await env.sender.close()
    assert env.operator.closed and env.receiver.closed
