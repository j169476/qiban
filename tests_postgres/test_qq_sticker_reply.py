import base64

import pytest

from companion_core.errors import AssistantError
from companion_core.sticker_library import StickerLibrary
from companion_qq.voice_settings import VoiceSettings
from tests_postgres.test_qq_native_provider import native as native
from tests_postgres.test_sticker_library import picture


async def setup(native, monkeypatch, mode="text", fault=None):
    lib = StickerLibrary(native.store)
    item = lib.import_bytes(picture(), "打招呼", ["你好"], actor="test")["item"]
    item = lib.configure(item["id"], item["name"], item["tags"], True, item["revision"], actor="test")
    if mode == "voice":
        VoiceSettings(native.store).configure(True, 30, None, actor="test")
        from companion_qq import reply_material

        monkeypatch.setattr(
            reply_material,
            "synthesize_audio",
            lambda *a, **k: {
                "format": "SILK",
                "audio": base64.b64encode(b"\x02#!SILK_V3fixture").decode(),
                "actual_audio_ms": 3000,
            },
        )
    await native.module.runtime_poll()
    worker = native.workers[-1]
    worker.arrivals()
    await native.module.runtime_poll()
    event = (await native.module.runtime_poll())[0]
    generate = native.model.generate
    snapshots = []

    async def choose(instructions, context, *args, **kwargs):
        result = await generate(instructions, context, *args, **kwargs)
        if result.get("action"):
            snapshots.append(context["event_material"]["stickers"])
            result["action"]["arguments"] = {"sticker_id": item["id"]}
            if mode != "alone":
                result["action"]["arguments"]["text"] = "synthetic reply"
                if mode == "voice":
                    result["action"]["arguments"]["format"] = mode
            if fault == "unoffered":
                result["action"]["arguments"]["sticker_id"] = "f" * 32
            if fault == "changed":
                lib.configure(item["id"], "新名称", item["tags"], True, item["revision"], actor="test")
        return result

    monkeypatch.setattr(native.model, "generate", choose)
    original = worker.request
    commands, ids, cancelled = [], [], []

    def request(command, **args):
        commands.append(command)
        if command.startswith("prepare_"):
            identifier = str(9001 + len(ids))
            ids.append(identifier)
            if command == "prepare_image":
                assert base64.b64decode(args["image"]) == picture()
                if fault == "disable_after_prepare":
                    current = lib.get(item["id"])
                    lib.configure(
                        item["id"], current["name"], current["tags"], False, current["revision"], actor="test"
                    )
            return {"token": "a" * 64, "message_id": identifier}
        if command == "submit_text":
            worker.submits += 1
            if fault == "lost_image" and len(ids) == 2:
                raise TimeoutError("lost")
            return {"message_id": ids[-1], "reference": "native-seq:" + str(700 + len(ids))}
        if command == "cancel_text":
            cancelled.append(args["token"])
            return {"cancelled": True}
        return original(command, **args)

    monkeypatch.setattr(worker, "request", request)
    return event, worker, commands, snapshots, cancelled


@pytest.mark.parametrize("mode", ["text", "voice", "alone"])
async def test_formal_single_decision_sticker_combinations_and_no_resend(native, monkeypatch, mode):
    event, worker, commands, snapshots, _ = await setup(native, monkeypatch, mode)
    result = await native.core.handle(event)
    assert result["action"]["status"] == "succeeded"
    expected = 1 if mode == "alone" else 2
    assert worker.submits == expected and len(snapshots) == 1
    action = result["action"]["id"]
    journal = native.store.state("qq-reply-bundle:" + action)
    assert journal["status"] == "succeeded"
    assert all(part["status"] == "confirmed" for part in journal["parts"])
    assert "prepare_image" in commands
    if mode == "voice":
        assert "prepare_voice" in commands
    for part in journal["parts"]:
        for value in part["receipt"].values():
            assert native.store.state(native.module.sent_key("group", "10003", value)) == {"action": action}
    await native.core.handle(event)
    assert worker.submits == expected


@pytest.mark.parametrize(
    "fault,status,count",
    [
        ("unoffered", "cancelled", 0),
        ("changed", "failed", 1),
        ("disable_after_prepare", "failed", 1),
        ("lost_image", "uncertain", 2),
    ],
)
async def test_formal_sticker_mutation_receipt_loss_and_unoffered_id(
    native, monkeypatch, fault, status, count
):
    event, worker, commands, _, cancelled = await setup(native, monkeypatch, fault=fault)
    result = await native.core.handle(event)
    assert result["action"]["status"] == status
    assert worker.submits == count
    if fault == "disable_after_prepare":
        assert len(cancelled) == 1
    if fault != "unoffered":
        journal = native.store.state("qq-reply-bundle:" + result["action"]["id"])
        assert journal["parts"][0]["status"] == "confirmed"
        assert journal["parts"][1]["status"] == ("uncertain" if fault == "lost_image" else "not_submitted")
    if fault == "lost_image":
        assert worker.closed and native.module.tainted


async def test_voice_fallback_then_sticker_uses_original_text_and_records_format(native, monkeypatch):
    from companion_qq import reply_material

    event, worker, commands, _, _ = await setup(native, monkeypatch, mode="voice")

    def fail(*args, **kwargs):
        raise AssistantError("audio_duration", "too long")

    monkeypatch.setattr(reply_material, "synthesize_audio", fail)
    result = await native.core.handle(event)
    assert result["action"]["status"] == "succeeded" and worker.submits == 2
    assert "prepare_voice" not in commands and "prepare_text" in commands and "prepare_image" in commands
    assert result["action"]["receipt"]["format"] == "text"
    assert result["action"]["receipt"]["fallback"] == "audio_duration"


async def test_only_final_budget_candidates_are_bound(native, monkeypatch):
    event, worker, _, _, _ = await setup(native, monkeypatch)
    original = native.module.decision_context_ready

    def no_candidates(event, material):
        material["event_material"]["stickers"]["items"] = []
        original(event, material)

    monkeypatch.setattr(native.module, "decision_context_ready", no_candidates)
    result = await native.core.handle(event)
    assert result["action"]["status"] == "cancelled" and worker.submits == 0
    assert result["action"]["error"] == "sticker_not_offered"


async def test_cancel_after_confirmed_text_stops_image_preparation(native, monkeypatch):
    import asyncio
    import threading

    event, worker, commands, _, _ = await setup(native, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = worker.request

    def block(command, **args):
        result = original(command, **args)
        if command == "submit_text":
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(worker, "request", block)
    task = asyncio.create_task(native.core.handle(event))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.submits == 1 and "prepare_image" not in commands
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_transport_error_survives_cleanup_error(native, monkeypatch):
    event, worker, _, _, _ = await setup(native, monkeypatch, fault="lost_image")
    original = worker.close

    def fail_cleanup():
        original()
        raise AssistantError("qq_media_path", "cleanup failed")

    monkeypatch.setattr(worker, "close", fail_cleanup)
    try:
        result = await native.core.handle(event)
    finally:
        monkeypatch.setattr(worker, "close", original)
    assert result["action"]["status"] == "uncertain"
    detail = native.store.state("qq-native-failure:" + result["action"]["id"])
    assert detail["error"] == "TimeoutError" and detail["cleanup_error"] == "qq_media_path"
    assert result["action"]["error"] != "qq_media_path"


async def test_whole_operation_timeout_diagnostic_is_preserved(native, monkeypatch):
    event, worker, _, _, _ = await setup(native, monkeypatch, fault="lost_image")
    original = worker.request
    def diagnostic(command, **args):
        if command == "send_diagnostic":
            return {"phase": "native_return_timeout", "native_result": None, "event_status": 2}
        return original(command, **args)
    monkeypatch.setattr(worker, "request", diagnostic)
    result = await native.core.handle(event)
    assert result["action"]["status"] == "uncertain"
    detail = native.store.state("qq-native-failure:" + result["action"]["id"])
    assert detail["transport"] == {"phase": "native_return_timeout", "native_result": None, "event_status": 2}
