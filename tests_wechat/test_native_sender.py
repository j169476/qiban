"""Synthetic native endpoints and receipts; no Windows API or live client access."""
import asyncio
import ctypes
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_wechat.live_sender import DriverOutcome, SendProof
from companion_wechat.native.bridge import NativeBridge, Request, Status, validate_build
from companion_wechat.native.protocol import ProbeFailure
from companion_wechat.native_sender import NativeDriver, NativeProof, NativeSenderGateway
from tests_wechat.test_live_sender import env as env  # noqa: F401
from tests_wechat.test_live_sender import prepare


class Backend:
    def __init__(self):
        self.epoch = "synthetic-native-process"
        self.sequence = self.sends = self.cancels = 0
        self.pending = self.closed = self.dead = False
        self.outcomes = []
        self.before_submit = lambda: None

    def open(self):
        return self

    def check(self):
        if self.dead:
            raise RuntimeError("synthetic process exited")

    def prepare(self, request):
        self.check()
        self.sequence += 1

    def inspect(self):
        self.check()

    def submit_once(self):
        self.before_submit()
        self.sends += 1

    def status(self):
        value = {"sequence": self.sequence, "completed": 1, "error": 0,
                 "tasks": 0, "completions": 0, "callbacks": 0}
        return {**value, **(self.outcomes.pop(0) if self.outcomes else {})}

    def cancel(self):
        self.cancels += 1

    def close(self):
        self.closed = True


class Receipts:
    def __init__(self):
        self.begins = self.checks = self.discards = 0
        self.positive = True

    async def begin(self, request):
        self.begins += 1
        return object()

    async def confirm(self, request, token):
        self.checks += 1
        return DriverOutcome("confirmed", True) if self.positive else DriverOutcome("uncertain")

    async def discard(self, request, token):
        self.discards += 1


@pytest.fixture
def native(env):
    backend, receipts, clock = Backend(), Receipts(), [0.0]

    async def sleep(seconds):
        clock[0] += seconds
        await asyncio.sleep(0)

    driver = NativeDriver(lambda: backend, receipts, clock=lambda: clock[0], sleep=sleep)
    sender = NativeSenderGateway(env.store, env.receiver, env.permits, driver, env.session, timeout=25)
    return SimpleNamespace(backend=backend, receipts=receipts, driver=driver, sender=sender, clock=clock)


@pytest.mark.asyncio
async def test_native_receipt_latches_before_callback_and_repeat_action_is_not_sent(env, native):
    native.backend.outcomes = [{"completed": 0, "callbacks": 1}, {}]
    permit = await prepare(env, sender=native.sender)
    assert native.clock[0] == 3 and native.receipts.begins == 1
    result = await native.sender.commit(permit, guard=lambda: None)
    assert result.status == "succeeded" and native.backend.sends == 1
    assert native.receipts.checks == 1 and native.receipts.discards == 1
    assert (await native.sender.commit(permit, guard=lambda: None)) == result
    again = await prepare(env, sender=native.sender)
    assert (await native.sender.commit(again, guard=lambda: None)).status == "succeeded"
    assert native.backend.sends == 1


@pytest.mark.asyncio
async def test_unknown_receipt_is_never_resent_even_after_new_gateway(env, native):
    native.receipts.positive = False
    permit = await prepare(env, sender=native.sender)
    result = await native.sender.commit(permit, guard=lambda: None)
    assert result.status == "uncertain" and native.driver.tainted and native.backend.sends == 1
    replacement = NativeSenderGateway(env.store, env.receiver, env.permits, native.driver, env.session)
    again = await prepare(env, sender=replacement)
    assert (await replacement.commit(again, guard=lambda: None)).status == "uncertain"
    assert native.backend.sends == 1


@pytest.mark.asyncio
async def test_stop_after_prepare_prevents_native_commit_and_releases_receipt(env, native):
    ticket = await native.sender.prepare(env.request)
    permit = env.permits.issue(ticket, env.request)
    native.sender.revoke_generation(env.request.generation)
    assert (await native.sender.commit(permit, guard=lambda: None)).status == "cancelled"
    await native.sender.close()
    assert native.backend.sends == 0 and native.backend.cancels >= 1
    assert native.backend.closed and native.receipts.discards == 1


@pytest.mark.asyncio
async def test_stop_during_settle_never_prepares_or_sends(env, native):
    async def stop(_seconds):
        native.driver.revoke()
    native.driver.sleep = stop
    with pytest.raises(AssistantError):
        await native.driver.prepare(env.request)
    assert native.backend.sends == native.backend.sequence == 0
    assert native.receipts.discards == 1


@pytest.mark.asyncio
async def test_early_timer_wakeup_still_waits_for_full_watermark_settle(env, native):
    sleeps = []

    async def early(seconds):
        sleeps.append(seconds)
        native.clock[0] += max(0.01, seconds / 2)
        await asyncio.sleep(0)

    native.driver.sleep = early
    await native.driver.prepare(env.request)
    assert native.clock[0] >= 3 and len(sleeps) > 1 and native.backend.sequence == 1


@pytest.mark.asyncio
async def test_new_process_can_reconnect_before_any_submission(env, native):
    first = await native.driver.prepare(env.request)
    await native.driver.discard(first)
    old = native.backend
    old.dead = True
    replacement = Backend()
    replacement.epoch = "synthetic-restarted-process"
    native.driver.factory = lambda: replacement
    next_proof = await native.driver.prepare(replace(env.request, action_id="new-action"))
    assert old.closed and next_proof.client_epoch != first.client_epoch
    assert old.sends == replacement.sends == 0


@pytest.mark.parametrize("text", ["a" * 301, "😀" * 256, "line\nbreak", "nul\0", "\u2028"])
def test_native_limits_reject_before_client_access(env, native, text):
    with pytest.raises(AssistantError):
        native.sender._request_valid(replace(env.request, text=text))
    assert native.driver.backend is None


def test_ui_proof_cannot_authorize_native_send(env):
    proof = SendProof(env.request.account_identity, env.request.target_ref, "native", "target", "draft", True, True, False)
    with pytest.raises(AssistantError):
        NativeSenderGateway._proof(env.request, proof)
    forged = NativeProof(env.request.account_identity, env.request.target_ref, "native", "wrong-hash", 1, True)
    with pytest.raises(AssistantError):
        NativeSenderGateway._proof(env.request, forged)


def test_remote_timeout_keeps_parameter_memory_until_client_exit():
    calls = []
    kernel = SimpleNamespace(CreateRemoteThread=lambda *args: 9, WaitForSingleObject=lambda *args: 258)
    windows = SimpleNamespace(k=kernel, api=SimpleNamespace(close=lambda h: calls.append(h)))
    bridge = NativeBridge("unused.dll", "synthetic", {"owner_main": "owner"}, windows=windows)
    bridge.handle, bridge.base, bridge.allocations = 8, 200, [(100, 1632)]
    with pytest.raises(ProbeFailure, match="call_uncertain"):
        bridge.call(200, 100)
    with pytest.raises(ProbeFailure, match="previous_call_uncertain"):
        bridge.call(200, 100)
    bridge.close()
    assert calls == [9, 8] and bridge.allocations == [(100, 1632)]


def test_static_abi_and_built_exports():
    assert ctypes.sizeof(Request) == 1632 and ctypes.sizeof(Status) == 40
    path = Path(__file__).resolve().parents[1] / ".dev/wechat/native-module-build/xln_native_module.dll"
    if not path.exists():
        pytest.skip("optional local native build absent")
    assert set(validate_build(path)) == {"XlnOpen", "XlnPrepare", "XlnCommit", "XlnCancel", "XlnStatus", "XlnClose"}
