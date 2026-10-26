"""Synthetic drivers only; this module cannot operate a client or send messages."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.storage import Store
from companion_wechat.bindings import BindingRegistry, ConversationBinding
from companion_wechat.contracts import Connection, SendRequest, require
from companion_wechat.gateway import PermitAuthority
from companion_wechat.live_sender import DriverOutcome, LiveSenderGateway, SendProof
from tests_wechat.test_receiver_journal import EncryptedTestStore


class Session:
    def __init__(self, store):
        self.store, self.enabled = store, True
        self.key = "synthetic-live-reservations"

    def check_request(self, request):
        require(self.enabled, "core_before_send_cancelled")

    def reserve(self, request, *, db=None):
        assert db is not None
        self.check_request(request)
        saved = self.store.state(self.key, {}, db)
        require(saved.get(request.conversation) in (None, request.action_id), "wechat_session_limit")
        saved[request.conversation] = request.action_id
        self.store.set_state(self.key, saved, db)

    def revoke_generation(self, generation):
        self.enabled = False


class Receiver:
    def __init__(self, store):
        self.bindings = BindingRegistry(store, "synthetic-logical", "synthetic-self")
        if not store.state(self.bindings.key)["conversations"]:
            self.bindings.bind(ConversationBinding("owner_main", "synthetic-owner", "private", "synthetic-owner"))
            self.bindings.bind(ConversationBinding("family", "synthetic-family", "group"))
        self.closed = False

    def connection(self):
        # Deliberately ready even when the native client is unavailable: this is
        # the durable receive epoch and must never count as send permission.
        return Connection("synthetic-self", "durable-inbox-epoch", not self.closed)

    async def close(self):
        self.closed = True


class Driver:
    def __init__(self):
        self.proof = SendProof("synthetic-self", "synthetic-owner", "native-login-1",
                               "native-private-target", "empty-draft-1", True, True, False)
        self.prepares = self.inspections = 0
        self.sent = []
        self.closed = False
        self.inspect_hook = lambda: None
        self.before_submit = lambda: None
        self.after_submit = lambda: None
        self.outcome = DriverOutcome("confirmed", client_recorded=True)
        self.confirmation = DriverOutcome("uncertain")
        self.confirm_started = asyncio.Event()
        self.confirm_release = None

    async def prepare(self, request):
        self.prepares += 1
        return self.proof

    def inspect(self, request):
        self.inspections += 1
        self.inspect_hook()
        return self.proof

    def submit_once(self, request, proof):
        self.before_submit()
        # This final native recheck is the driver's responsibility. A changed
        # process/window/draft must not be sent to, even after service checks.
        if self.proof != proof:
            return DriverOutcome("rejected")
        if self.outcome.status != "rejected":
            self.sent.append(request)
        self.after_submit()
        return self.outcome

    async def confirm(self, request, proof, initial):
        self.confirm_started.set()
        if self.confirm_release is not None:
            await self.confirm_release.wait()
        return self.confirmation

    async def close(self):
        self.closed = True


@pytest.fixture
def env(tmp_path):
    store = EncryptedTestStore(tmp_path / "live")
    receiver, driver, now = Receiver(store), Driver(), [100.0]
    session = Session(store)
    permits = PermitAuthority(b"synthetic-live-authority-key-0000", clock=lambda: now[0])
    sender = LiveSenderGateway(store, receiver, permits, driver, session)
    request = SendRequest("synthetic-action", "synthetic-generation", "synthetic-self", "durable-inbox-epoch",
                          "owner_main", 1, "synthetic-owner", "synthetic short reply")
    return SimpleNamespace(store=store, receiver=receiver, driver=driver, session=session,
                           permits=permits, sender=sender, request=request, now=now)


async def prepare(env, request=None, sender=None):
    request, sender = request or env.request, sender or env.sender
    ticket = await sender.prepare(request)
    return env.permits.issue(ticket, request)


def restarted(env, *, driver=None):
    store = EncryptedTestStore(env.store.root)
    return LiveSenderGateway(store, Receiver(store), env.permits, driver or Driver(), Session(store))


def deny():
    raise AssistantError("core_before_send_cancelled", "synthetic stop")


@pytest.mark.asyncio
async def test_prepared_commit_issues_only_after_queue_and_slow_guards(env, monkeypatch):
    ticket = await env.sender.prepare(env.request)
    issued = []
    original = env.permits.issue

    def issue(prepared, request):
        issued.append(env.now[0])
        return original(prepared, request)

    def slow_guard():
        env.now[0] += 3

    monkeypatch.setattr(env.permits, "issue", issue)
    await env.sender._commit_lock.acquire()
    task = asyncio.create_task(env.sender.commit_prepared(ticket, guard=slow_guard))
    await asyncio.sleep(0)
    env.now[0] += 10
    assert issued == [] and env.driver.sent == []
    env.sender._commit_lock.release()
    result = await task
    assert result.status == "succeeded" and issued == [119.0]
    assert env.permits.lifetime_seconds == 2 and len(env.driver.sent) == 1
    assert await env.sender.commit_prepared(ticket, guard=deny) == result
    again = restarted(env)
    assert await again.commit_prepared(ticket, guard=deny) == result
    assert len(env.driver.sent) == 1 and not again.driver.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["digest", "revoked", "last-guard", "target", "account"])
async def test_prepared_commit_cannot_bypass_final_authority(env, fault, monkeypatch):
    ticket = await env.sender.prepare(env.request)
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        env.now[0] += 3
        if fault == "last-guard" and checks == 3:
            deny()

    if fault == "digest":
        ticket = replace(ticket, request_digest="different")
    elif fault == "revoked":
        env.sender.revoke_generation(env.request.generation)
    elif fault == "target":
        env.driver.proof = replace(env.driver.proof, target_token="changed")
    elif fault == "account":
        monkeypatch.setattr(env.receiver, "connection", lambda: Connection("different", "durable-inbox-epoch", True))
    result = await env.sender.commit_prepared(ticket, guard=guard)
    assert result.status == "cancelled" and env.driver.sent == []


@pytest.mark.asyncio
async def test_prepared_commit_never_accepts_expired_signed_permit(env):
    permit = await prepare(env)
    env.now[0] += 10
    assert (await env.sender.commit_prepared(permit, guard=lambda: None)).status == "cancelled"
    assert (await env.sender.commit(permit, guard=lambda: None)).status == "cancelled"
    assert not env.driver.sent


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,code', [('expired', 'wechat_permit_expired'),
    ('digest', 'wechat_permit_request_mismatch'), ('signature', 'wechat_permit_signature_invalid'),
    ('clock', 'wechat_permit_clock_invalid')])
async def test_permit_failures_have_distinct_safe_codes(env, fault, code):
    permit = await prepare(env)
    if fault == 'expired':
        env.now[0] += 3
    elif fault == 'digest':
        permit = replace(permit, request_digest='different')
    elif fault == 'signature':
        permit = replace(permit, signature='0' * 64)
    else:
        env.now[0] -= 3
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.error == code and result.status == 'cancelled' and not env.driver.sent


def test_unconfigured_preflight_is_pure_and_fails_closed(env):
    sender = LiveSenderGateway(env.store, env.receiver, env.permits, None, env.session)
    assert sender.driver is None and sender.available is False
    with pytest.raises(AssistantError) as caught:
        sender.preflight()
    assert caught.value.code == "wechat_live_driver_missing"
    assert not env.driver.prepares and not env.driver.inspections
    sender.revoke_generation(None)


def test_plaintext_store_cannot_enable_live_sending(env, tmp_path):
    sender = LiveSenderGateway(Store(tmp_path / "unencrypted"), env.receiver, env.permits, env.driver, env.session)
    with pytest.raises(AssistantError) as caught:
        sender.preflight()
    assert caught.value.code == "wechat_send_store_unencrypted"
    assert not env.driver.prepares and not env.driver.inspections


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "  ", "line\nline", "line\rline", "tab\ttext", "NUL\x00", "\x7f",
                                  "x\u2028y", "x\u2029y", "\ud800", "x" * 2049, "你" * 683])
async def test_short_single_line_utf8_bound_before_any_driver_call(env, text):
    with pytest.raises(AssistantError) as caught:
        await env.sender.prepare(replace(env.request, text=text))
    assert caught.value.code == "wechat_send_text_invalid"
    assert env.driver.prepares == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"ready": False}, {"draft_empty": False}, {"manual_takeover": True},
    {"account_identity": "different-account"}, {"target_ref": "other-target"},
    {"client_epoch": "durable-inbox-epoch"}, {"draft_token": ""},
])
async def test_receive_ready_cannot_substitute_for_live_target_proof(env, change):
    env.driver.proof = replace(env.driver.proof, **change)
    assert env.receiver.connection().ready
    with pytest.raises(AssistantError) as caught:
        await env.sender.prepare(env.request)
    assert caught.value.code == "wechat_send_target_unverified"
    assert not env.driver.sent


@pytest.mark.asyncio
async def test_confirmed_result_is_encrypted_durable_and_replayed_even_after_expiry(env):
    permit = await prepare(env)
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "succeeded"
    assert result.receipt == {"client_recorded": True, "action_id": env.request.action_id}
    assert len(env.driver.sent) == 1
    env.now[0] += 10
    again = restarted(env)
    assert await again.commit(permit, guard=deny) == result
    assert not again.driver.sent and not again.driver.prepares
    with env.store.db() as db:
        records = db.execute("SELECT value FROM state WHERE key LIKE ?", ("wechat-live-%",)).fetchall()
    assert len(records) == 2
    assert all(record[0].startswith("enc1:") and env.request.text not in record[0] for record in records)


@pytest.mark.asyncio
async def test_same_action_new_ticket_after_restart_never_submits_again(env):
    permit = await prepare(env)
    first = await env.sender.commit(permit, guard=lambda: None)
    again = restarted(env)
    second_permit = await prepare(env, sender=again)
    assert await again.commit(second_permit, guard=lambda: None) == first
    assert len(env.driver.sent) == 1 and not again.driver.sent and not again.driver.prepares
    with pytest.raises(AssistantError) as caught:
        await again.prepare(replace(env.request, text="changed payload"))
    assert caught.value.code == "wechat_action_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["target", "client", "draft", "binding", "generation"])
async def test_changes_between_prepare_and_commit_never_submit(env, change):
    permit = await prepare(env)
    if change == "target":
        env.driver.proof = replace(env.driver.proof, target_token="different-control")
    elif change == "client":
        env.driver.proof = replace(env.driver.proof, client_epoch="native-login-2")
    elif change == "draft":
        env.driver.proof = replace(env.driver.proof, draft_token="user-edited-and-cleared")
    elif change == "binding":
        binding = env.receiver.bindings.get("owner_main")
        env.receiver.bindings.bind(replace(binding, enabled=False), expected_revision=1)
    else:
        env.sender.revoke_generation(env.request.generation)
    assert (await env.sender.commit(permit, guard=lambda: None)).status == "cancelled"
    assert not env.driver.sent


@pytest.mark.asyncio
async def test_revoke_while_waiting_for_commit_queue_is_checked_after_queue(env):
    permit = await prepare(env)
    await env.sender._commit_lock.acquire()
    task = asyncio.create_task(env.sender.commit(permit, guard=lambda: None))
    await asyncio.sleep(0)
    env.sender.revoke_generation(env.request.generation)
    env.sender._commit_lock.release()
    assert (await task).status == "cancelled"
    assert not env.driver.sent


@pytest.mark.asyncio
async def test_revocation_during_last_inspection_is_rechecked(env):
    permit = await prepare(env)

    def changed():
        if env.driver.inspections == 2:
            env.session.enabled = False

    env.driver.inspect_hook = changed
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "cancelled" and not env.driver.sent


@pytest.mark.asyncio
async def test_driver_rechecks_native_target_at_actual_submission(env):
    permit = await prepare(env)
    env.driver.before_submit = lambda: setattr(env.driver, "proof", replace(env.driver.proof, target_token="moved"))
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "failed" and result.error == "wechat_provider_rejected"
    assert not env.driver.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["expired", "tampered", "guard-expiry"])
async def test_invalid_or_expired_permit_never_submits(env, fault):
    permit = await prepare(env)

    def guard():
        if fault == "guard-expiry":
            env.now[0] += 3

    if fault == "expired":
        env.now[0] += 3
    elif fault == "tampered":
        permit = replace(permit, signature="0" * 64)
    assert (await env.sender.commit(permit, guard=guard)).status == "cancelled"
    assert not env.driver.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,confirmed,expected", [
    (DriverOutcome("confirmed", True), DriverOutcome("uncertain"), "succeeded"),
    (DriverOutcome("confirmed", False), DriverOutcome("uncertain"), "uncertain"),
    (DriverOutcome("rejected"), DriverOutcome("uncertain"), "failed"),
    (DriverOutcome("rejected", True), DriverOutcome("uncertain"), "uncertain"),
    (DriverOutcome("uncertain"), DriverOutcome("confirmed", True), "succeeded"),
    (DriverOutcome("uncertain"), DriverOutcome("rejected"), "uncertain"),
])
async def test_only_verified_echo_can_confirm_and_missing_echo_is_not_rejection(env, initial, confirmed, expected):
    env.driver.outcome, env.driver.confirmation = initial, confirmed
    result = await env.sender.commit(await prepare(env), guard=lambda: None)
    assert result.status == expected
    assert "delivered" not in result.receipt


@pytest.mark.asyncio
async def test_cancel_after_submission_is_uncertain_and_cannot_repeat(env):
    env.driver.outcome = DriverOutcome("uncertain")
    env.driver.confirm_release = asyncio.Event()
    permit = await prepare(env)
    task = asyncio.create_task(env.sender.commit(permit, guard=lambda: None))
    await env.driver.confirm_started.wait()
    task.cancel()
    result = await task
    assert result.status == "uncertain" and len(env.driver.sent) == 1
    assert (await restarted(env).commit(permit, guard=lambda: None)).status == "uncertain"


@pytest.mark.asyncio
async def test_confirmation_timeout_is_uncertain_without_second_submission(env):
    env.driver.outcome = DriverOutcome("uncertain")
    env.driver.confirm_release = asyncio.Event()
    env.sender.timeout = 0.01
    permit = await prepare(env)
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "uncertain" and len(env.driver.sent) == 1
    assert (await restarted(env).commit(permit, guard=lambda: None)).status == "uncertain"


@pytest.mark.asyncio
async def test_two_gateway_instances_cannot_submit_same_action_concurrently(env):
    env.driver.outcome, env.driver.confirmation = DriverOutcome("uncertain"), DriverOutcome("confirmed", True)
    env.driver.confirm_release = asyncio.Event()
    permit = await prepare(env)
    first = asyncio.create_task(env.sender.commit(permit, guard=lambda: None))
    await env.driver.confirm_started.wait()
    other = restarted(env)
    second_permit = await prepare(env, sender=other)
    assert (await other.commit(second_permit, guard=lambda: None)).status == "uncertain"
    assert not other.driver.sent
    env.driver.confirm_release.set()
    assert (await first).status == "succeeded" and len(env.driver.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["before-effect", "after-effect"])
async def test_crash_after_durable_intent_never_replays_submission(env, position):
    class ProcessCrash(BaseException):
        pass

    permit = await prepare(env)

    def crash():
        raise ProcessCrash()

    if position == "after-effect":
        env.driver.after_submit = crash
    else:
        env.driver.inspect_hook = lambda: crash() if env.driver.inspections == 2 else None
    with pytest.raises(ProcessCrash):
        await env.sender.commit(permit, guard=lambda: None)
    assert env.store.state(env.sender._action_key(env.request))["status"] == "submitting"
    other = restarted(env)
    assert (await other.commit(permit, guard=lambda: None)).status == "uncertain"
    second = await prepare(env, sender=other)
    assert (await other.commit(second, guard=lambda: None)).status == "uncertain"
    assert not other.driver.sent
    assert len(env.driver.sent) == int(position == "after-effect")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["submitting", "finished"])
async def test_journal_failure_cannot_cause_untracked_or_repeated_send(env, monkeypatch, stage):
    permit = await prepare(env)
    original = env.store.set_state

    def interrupted(key, value, db=None):
        original(key, value, db)
        if key == env.sender._action_key(env.request) and value["status"] == stage:
            raise RuntimeError("PRIVATE native value must never escape")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "set_state", interrupted)
        result = await env.sender.commit(permit, guard=lambda: None)
    assert "PRIVATE" not in repr(result)
    if stage == "submitting":
        assert result.status == "cancelled" and not env.driver.sent
        assert env.store.state(env.sender._action_key(env.request)) is None
        assert env.store.state(env.session.key) is None
    else:
        assert result.status == "uncertain" and len(env.driver.sent) == 1
        assert env.store.state(env.sender._action_key(env.request))["status"] == "submitting"
        assert (await restarted(env).commit(permit, guard=lambda: None)).status == "uncertain"


@pytest.mark.asyncio
async def test_native_exception_after_submit_is_redacted_and_never_retried(env):
    def raised():
        raise RuntimeError("PRIVATE WINDOWS PATH")

    env.driver.after_submit = raised
    permit = await prepare(env)
    result = await env.sender.commit(permit, guard=lambda: None)
    assert result.status == "uncertain" and "PRIVATE" not in repr(result)
    assert (await restarted(env).commit(permit, guard=lambda: None)).status == "uncertain"
    assert len(env.driver.sent) == 1


@pytest.mark.asyncio
async def test_discard_and_close_never_submit_prepared_messages(env):
    permit = await prepare(env)
    await env.sender.discard(permit.ticket)
    assert (await env.sender.commit(permit, guard=lambda: None)).status != "succeeded"
    await env.sender.close()
    assert env.driver.closed and env.receiver.closed
    assert not env.sender.connection().ready and not env.driver.sent
