"""Offline boundaries for the explicit native experiment; never load a DLL or send."""
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_wechat.bindings import ConversationBinding
from companion_wechat.contracts import SendRequest
from companion_wechat.live_sender import DriverOutcome
from scripts.wechat_native_probe.__main__ import experiment
from scripts.wechat_native_probe.protocol import (
    DLL,
    PROFILES,
    ProbeFailure,
    fingerprint,
    pe_exports,
    personal,
    validate_request,
)
from scripts.wechat_native_probe.win32 import Bridge, Windows


def fixture_request():
    request = SendRequest("action", "generation", "identity", "epoch", "owner_main", 1,
                          "wxid_main", "XLN_NATIVE_PROBE_" + "a" * 32)
    enrollment = {"self_username": "wxid_bot", "account_id": "identity",
                  "targets": {"owner_main": {"username": "wxid_main"}}}
    binding = ConversationBinding("owner_main", "wxid_main", "private", "wxid_main")
    return request, enrollment, binding


def test_exact_private_binding():
    validate_request(*fixture_request())


@pytest.mark.parametrize("changes", [
    {"conversation": "family"}, {"target_ref": "123@chatroom"}, {"target_ref": "filehelper"},
    {"target_ref": "wxid_other"}, {"target_ref": "wxid_bot"}, {"account_identity": "other"},
    {"revision": 2}, {"text": "arbitrary message"}, {"text": "XLN_NATIVE_PROBE_" + "f" * 31},
    {"text": "XLN_NATIVE_PROBE_" + "f" * 32 + "\0"},
])
def test_refuses_scope_or_text_changes(changes):
    request, enrollment, binding = fixture_request()
    with pytest.raises(ProbeFailure):
        validate_request(replace(request, **changes), enrollment, binding)


@pytest.mark.parametrize("value", ["", "filehelper", "gh_test", "1@chatroom", "a\0b", "a" * 128, "中文"])
def test_rejects_nonpersonal(value):
    assert not personal(value)


def test_disabled_binding():
    request, enrollment, binding = fixture_request()
    with pytest.raises(ProbeFailure):
        validate_request(request, enrollment, replace(binding, enabled=False))


def test_unknown_core_rejected_without_loading(tmp_path):
    file = tmp_path / "Weixin.dll"
    file.write_bytes(b"different version")
    with pytest.raises(ProbeFailure, match="unsupported_core"):
        fingerprint(file)


@pytest.mark.parametrize("data", [b"", b"MZ", bytes(512), b"MZ" + bytes(1022)])
def test_rejects_invalid_pe(data):
    with pytest.raises(ProbeFailure):
        pe_exports(data)


def test_local_build_has_only_three_exports():
    if not DLL.exists():
        pytest.skip("MSVC-built optional experimental DLL absent")
    assert set(pe_exports(DLL.read_bytes())) == {"XlnInspect", "XlnSendOnce", "XlnStatus"}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("mutation", [None, "getter", "timestamp", "identity_changed"])
def test_account_chain_matches_exact_build_and_detects_changes(profile, mutation):
    base, app, context, buffer = 0x100000, 0x20000000, 0x30000000, 0x40000000
    global_rva, app_table, app_getter, context_table, identity_getter = profile["account_rvas"]
    body = b"wxid_test_bound_account"
    memory = {
        base + 0x3C: struct.pack("<I", 128), base + 128: b"PE\0\0",
        base + 136: struct.pack("<I", profile["timestamp"] if mutation != "timestamp" else 1),
        base + 208: struct.pack("<I", profile["image_size"]),
        base + global_rva: struct.pack("<Q", app), app: struct.pack("<Q", base + app_table),
        base + app_table + 0x60: struct.pack("<Q", base + app_getter),
        app + 0x68: struct.pack("<Q", context), context: struct.pack("<Q", base + context_table),
        base + context_table + 0x18: struct.pack("<Q", base + identity_getter),
        base + identity_getter: b"\x48\x8d\x41\x48\xc3" if mutation != "getter" else b"\0" * 5,
        context + 0x48: struct.pack("<QQQQ", buffer, 0, len(body), len(body)), buffer: body,
    }
    reads = []

    def read(handle, address, size):
        reads.append(address)
        if mutation == "identity_changed" and address == buffer and reads.count(buffer) == 2:
            return b"x" * size
        return memory[address][:size]

    windows = object.__new__(Windows)
    windows.read = read
    if mutation:
        with pytest.raises(ProbeFailure):
            windows.account(1, base)
    else:
        assert windows.account(1, base) == body.decode()


def test_remote_timeout_keeps_parameters_and_no_unload():
    actions = []
    kernel = SimpleNamespace(CreateRemoteThread=lambda *args: 9, WaitForSingleObject=lambda *args: 258)
    windows = SimpleNamespace(k=kernel, api=SimpleNamespace(close=lambda h: actions.append(("close", h))))
    bridge = Bridge(windows, 1, 2, "wxid_bot", 3, 4)
    bridge.handle, bridge.base, bridge.allocations = 8, 200, [(100, 24)]
    with pytest.raises(ProbeFailure, match="call_uncertain"):
        bridge.call(200, 100)
    assert bridge.pending
    with pytest.raises(ProbeFailure, match="previous_call_uncertain"):
        bridge.call(200, 100)
    bridge.close()
    assert actions == [("close", 9), ("close", 8)]  # No free/unload API even supplied.


def test_repeat_submit_is_rejected_even_if_first_call_is_unknown():
    bridge = Bridge(None, 1, 2, "wxid_bot", 3, 4)
    bridge.base, bridge.exports = 10, {"XlnSendOnce": 5}
    bridge.check = lambda: None
    calls = []

    def call(*args):
        calls.append(args)
        raise ProbeFailure("call_uncertain")

    bridge.call = call
    with pytest.raises(ProbeFailure, match="call_uncertain"):
        bridge.submit_once()
    with pytest.raises(ProbeFailure, match="already_attempted"):
        bridge.submit_once()
    assert len(calls) == 1


class FakeBridge:
    def __init__(self, *, callback=True, focus=True, failure=False):
        self.pid, self.hwnd, self.foreground = 1, 2, 3
        self.calls, self.attempted, self.pending, self.closed = [], False, False, False
        self.callback, self.failure = callback, failure
        self.w = SimpleNamespace(focus=lambda *args: {
            "foreground": 3 if focus else 4, "minimized": focus, "target_foreground": False})

    def load(self): self.calls.append("load")
    def inspect(self, *args): self.calls.append("inspect")
    def submit_once(self):
        self.calls.append("submit")
        self.attempted = True
        if self.failure:
            self.pending = True
            raise ProbeFailure("call_uncertain")
        return 0
    def status(self):
        return {"stage": 3, "error": 0, "completed": int(self.callback),
                "tasks": 0, "completions": 0, "callbacks": 0, "attempted": 1}
    def close(self): self.closed = True


class FakeReceipts:
    def __init__(self, recorded=True): self.recorded, self.discarded = recorded, False
    async def begin(self, request): return "token"
    async def confirm(self, request, token):
        return DriverOutcome("confirmed" if self.recorded else "uncertain", self.recorded)
    async def discard(self, request, token): self.discarded = True


@pytest.mark.asyncio
@pytest.mark.parametrize("callback,recorded,focus,success", [
    (True, True, True, True), (False, True, True, False),
    (True, False, True, False), (True, True, False, False),
])
async def test_requires_callback_receipt_and_background(callback, recorded, focus, success):
    bridge, receipts = FakeBridge(callback=callback, focus=focus), FakeReceipts(recorded)
    result = await experiment(bridge, receipts, fixture_request()[0], timeout=0.05, settle_seconds=0)
    assert result["success"] is success
    assert bridge.calls == ["load", "inspect", "submit"]
    assert bridge.closed and receipts.discarded
    assert "wxid" not in str(result) and "XLN_NATIVE_PROBE_" not in str(result)


@pytest.mark.asyncio
async def test_unknown_dispatch_never_retries_and_cleans_receiver_token():
    bridge, receipts = FakeBridge(failure=True), FakeReceipts()
    result = await experiment(bridge, receipts, fixture_request()[0], timeout=0.01, settle_seconds=0)
    assert not result["success"] and result["bridge_resident"]
    assert bridge.calls.count("submit") == 1 and bridge.closed and receipts.discarded


@pytest.mark.asyncio
async def test_confirmed_receipt_is_latched_when_native_callback_arrives_later():
    bridge, receipts = FakeBridge(callback=False), FakeReceipts()
    checks = []

    async def confirm(request, token):
        checks.append(token)
        # The first status read was pending; the completion arrives while the DB
        # check is running. The worker has now consumed this single-use receipt.
        bridge.callback = True
        return DriverOutcome("confirmed" if len(checks) == 1 else "uncertain", len(checks) == 1)

    receipts.confirm = confirm
    result = await experiment(bridge, receipts, fixture_request()[0], timeout=2, settle_seconds=0)
    assert result["success"] and result["client_recorded"] and result["native_completed"]
    assert checks == ["token"] and result["receipt_checks"] == 1
    assert bridge.calls.count("submit") == 1


@pytest.mark.asyncio
async def test_settle_happens_after_watermark_and_before_bridge_load():
    import time
    bridge, receipts = FakeBridge(), FakeReceipts()
    times = {}

    async def begin(request):
        times["watermark"] = time.monotonic()
        return "token"

    def load():
        times["load"] = time.monotonic()

    receipts.begin, bridge.load = begin, load
    result = await experiment(bridge, receipts, fixture_request()[0], timeout=1, settle_seconds=0.03)
    assert result["success"] and times["load"] - times["watermark"] >= 0.03
