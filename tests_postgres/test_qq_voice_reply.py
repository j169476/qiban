import asyncio
import base64
import secrets
import threading
import time

import pytest

from companion_core.errors import AssistantError
from companion_qq.voice_settings import VoiceSettings
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service
from tests_postgres.test_qq_native_provider import native as native


def test_voice_settings_default_cas_admin_lock_and_dedup(service):
    initial = call(service, "media.voice")["result"]
    assert initial == {"enabled": False, "seconds": 30, "revision": None}
    args = {"enabled": True, "seconds": 60, "expected": None}
    key = secrets.token_hex(16)
    saved = call(service, "media.voice.configure", args, request_id=key)
    assert saved["ok"] and saved["result"]["seconds"] == 60
    assert call(service, "media.voice.configure", args, request_id=key) == saved
    assert call(service, "media.voice.configure", args)["error"] == "edit_conflict"
    assert call(service, "media.list", {"after": "", "limit": 50})["result"]["voice"] == saved["result"]
    service.enable_lock("test-password-123456")
    service.lock()
    assert call(service, "media.voice")["error"] == "desktop_locked"


async def ready(native, monkeypatch):
    VoiceSettings(native.store).configure(True, 30, None, actor="test")
    await native.module.runtime_poll()
    worker = native.workers[-1]
    worker.arrivals()
    await native.module.runtime_poll()
    event = (await native.module.runtime_poll())[0]
    generate = native.model.generate

    async def voice(*args, **kwargs):
        result = await generate(*args, **kwargs)
        if result.get("action"):
            result["action"]["arguments"]["format"] = "voice"
        return result

    monkeypatch.setattr(native.model, "generate", voice)
    request = worker.request
    commands = []

    def handle(command, **args):
        commands.append(command)
        if command == "prepare_voice":
            assert args["audio_ms"] == 3000
            return {"token": "a" * 64, "message_id": "9001"}
        return request(command, **args)

    monkeypatch.setattr(worker, "request", handle)
    return event, worker, commands


@pytest.mark.parametrize("fail", [False, True])
async def test_one_model_decision_selects_voice_or_exact_text_fallback_and_records_receipt(
    native, monkeypatch, fail
):
    from companion_qq import reply_material

    event, worker, commands = await ready(native, monkeypatch)
    synthesis = []

    def generate(text, *, max_audio_ms, guard):
        guard()
        synthesis.append((text, max_audio_ms))
        if fail:
            raise AssistantError("audio_process_timeout", "failed")
        return {
            "format": "SILK",
            "audio": base64.b64encode(b"\x02#!SILK_V3fixture").decode(),
            "actual_audio_ms": 3000,
        }

    monkeypatch.setattr(reply_material, "synthesize_audio", generate)
    result = await native.core.handle(event)
    assert result["action"]["status"] == "succeeded"
    assert worker.submits == 1 and synthesis == [("synthetic reply", 30000)]
    assert len([c for c in native.model.calls if "event_material" in c]) == 1
    assert ("prepare_text" if fail else "prepare_voice") in commands
    record = native.store.state("qq-native-send:" + result["action"]["id"])
    assert record["format"] == ("text" if fail else "voice")
    assert record["fallback"] == ("audio_process_timeout" if fail else None)
    assert record["status"] == "confirmed"


async def test_lost_voice_receipt_never_calls_text_fallback(native, monkeypatch):
    from companion_qq import reply_material

    event, worker, commands = await ready(native, monkeypatch)
    monkeypatch.setattr(
        reply_material,
        "synthesize_audio",
        lambda *a, **k: {
            "format": "SILK",
            "audio": base64.b64encode(b"\x02#!SILK_V3fixture").decode(),
            "actual_audio_ms": 3000,
        },
    )
    original = worker.request

    def lose(command, **args):
        if command == "submit_text":
            worker.submits += 1
            raise TimeoutError("receipt lost")
        return original(command, **args)

    monkeypatch.setattr(worker, "request", lose)
    result = await native.core.handle(event)
    assert result["action"]["status"] == "uncertain" and worker.submits == 1
    assert "prepare_text" not in commands
    record = native.store.state("qq-native-send:" + result["action"]["id"])
    assert record["status"] == "attempted" and record["format"] == "voice"


async def test_cancel_during_synthesis_never_submits(native, monkeypatch):
    from companion_qq import reply_material

    event, worker, commands = await ready(native, monkeypatch)
    started, ended = threading.Event(), threading.Event()

    def generate(*args, guard, **kwargs):
        started.set()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                guard()
                time.sleep(0.01)
            pytest.fail("cancel did not stop synthesis")
        finally:
            ended.set()

    monkeypatch.setattr(reply_material, "synthesize_audio", generate)
    task = asyncio.create_task(native.core.handle(event))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ended.is_set() and worker.submits == 0
    assert "prepare_text" not in commands and "prepare_voice" not in commands
