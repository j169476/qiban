import base64
from dataclasses import asdict, replace

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.file_library import FileLibrary
from companion_core.media_originals import MediaOriginals
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_qq.file_replies import FileReplySettings, ReplyFiles
from tests_postgres.test_qq_native_provider import native as native
from tests_postgres.test_qq_sticker_reply import setup as sticker_setup


async def setup(native, monkeypatch, *, mode="text", fault=None):
    event, worker, commands, snapshots, cancelled = await sticker_setup(native, monkeypatch, "alone" if mode == "files_only" else mode)
    scope = native.module.scope(event)
    auth = ReplyAuthorizations(native.store).version(scope)
    library = FileLibrary(native.store)
    originals = MediaOriginals(native.store)
    content = ("测试文件🙂" * (50000 if fault == "size" else 1)).encode()
    items = []
    for index in range(2):
        item = library.import_bytes(content + str(index).encode(), f"资料{index}.txt", ["说明"], actor="test")["item"]
        item = library.configure(item["id"], item["name"], item["tags"], True, [asdict(scope)], item["revision"], actor="test")
        items.append("library:" + item["id"])
    identifier = stable_key("qq-native-attachment", "outgoing-file-test")
    original_event = Event("qq", scope.account_identity, "file-origin", ActorRef("qq", "10002", scope.account_identity),
        "group:" + scope.target_id, "2026-10-31T00:00:00+00:00", "之前收到的文件", "group",
        attachments=({"id": identifier, "kind": "file", "position": 0},))
    version = events.ingest(native.store, original_event)["version"]
    originals.put(scope, auth, original_event.id, version, identifier, b"original attachment", name="会话原件.txt", guard=lambda: None)
    items.insert(0, "attachment:" + identifier)
    if fault == "size":
        FileReplySettings(native.store).configure(3, 1, None, actor="test")
    generate = native.model.generate
    def disable(identifier):
        item = library.get(identifier.split(":", 1)[1])
        library.configure(item["id"], item["name"], item["tags"], False, item["scopes"], item["revision"], actor="test")
    async def choose(instructions, context, *args, **kwargs):
        result = await generate(instructions, context, *args, **kwargs)
        if result.get("action"):
            offered = context["event_material"]["files"]["items"]
            if fault != "budget":
                assert all(key in {item["id"] for item in offered} for key in items)
            result["action"]["arguments"]["file_ids"] = items if fault != "unoffered" else ["library:" + "f" * 32]
            if mode == "files_only":
                result["action"]["arguments"].pop("sticker_id")
            if fault == "origin_changed":
                events.ingest(native.store, replace(original_event, text="已改变来源"))
            if fault == "changed":
                disable(items[-1])
            if fault == "settings":
                FileReplySettings(native.store).configure(2, 20, None, actor="test")
        return result
    monkeypatch.setattr(native.model, "generate", choose)
    request = worker.request
    prepared_files = []
    def send(command, **args):
        if command == "prepare_file":
            prepared_files.append(args)
            assert args["name"] in {"资料0.txt", "资料1.txt", "会话原件.txt"}
            assert base64.b64decode(args["file"])
            if fault == "settings_prepared":
                FileReplySettings(native.store).configure(3, 21, None, actor="test")
            if fault == "disable_prepared" and len(prepared_files) == 2:
                disable(items[1])
        if command == "submit_text" and fault == "lost" and len(prepared_files) == 2:
            worker.submits += 1
            raise TimeoutError("lost file receipt")
        return request(command, **args)
    monkeypatch.setattr(worker, "request", send)
    return event, worker, items, prepared_files, commands, cancelled


@pytest.mark.parametrize("mode", ["text", "voice", "alone", "files_only"])
async def test_formal_multi_file_reply_with_same_decision_and_per_part_receipts(native, monkeypatch, mode):
    event, worker, ids, prepared, commands, _ = await setup(native, monkeypatch, mode=mode)
    result = await native.core.handle(event)
    assert result["action"]["status"] == "succeeded"
    assert len(prepared) == 3 and worker.submits == (3 if mode == "files_only" else 4 if mode == "alone" else 5)
    assert [part["kind"] for part in result["action"]["receipt"]["parts"]] == ([] if mode in {"alone", "files_only"} else [mode]) + ["file"] * 3 + ([] if mode == "files_only" else ["sticker"])
    assert [file["name"] for file in prepared] == ["会话原件.txt", "资料0.txt", "资料1.txt"]
    with native.store.db() as db:
        assert db.execute("SELECT count(*) FROM media_original_holds").fetchone()[0] == 0
    count = worker.submits
    await native.core.handle(event)
    assert worker.submits == count


@pytest.mark.parametrize("fault,status,count", [
    ("unoffered", "cancelled", 0), ("changed", "cancelled", 0), ("settings", "cancelled", 0),
    ("size", "cancelled", 0), ("origin_changed", "cancelled", 0), ("settings_prepared", "failed", 1), ("disable_prepared", "failed", 2), ("lost", "uncertain", 3)])
async def test_invalid_or_changed_files_and_lost_receipt_stop_remaining_parts(native, monkeypatch, fault, status, count):
    event, worker, ids, prepared, commands, cancelled = await setup(native, monkeypatch, fault=fault)
    result = await native.core.handle(event)
    assert result["action"]["status"] == status
    assert worker.submits == count
    assert "prepare_image" not in commands
    if fault == "disable_prepared":
        assert cancelled
    if fault == "lost":
        assert worker.closed and native.module.tainted
        parts = result["action"]["receipt"]["parts"]
        assert [part["status"] for part in parts] == ["confirmed", "confirmed", "uncertain", "not_submitted", "not_submitted"]
    with native.store.db() as db:
        assert db.execute("SELECT count(*) FROM media_original_holds").fetchone()[0] == 0


async def test_final_budget_removed_file_is_not_sendable(native, monkeypatch):
    event, worker, ids, prepared, commands, _ = await setup(native, monkeypatch, fault="budget")
    ready = native.module.decision_context_ready
    def filtered(event, material):
        material["event_material"]["files"]["items"] = []
        ready(event, material)
    monkeypatch.setattr(native.module, "decision_context_ready", filtered)
    result = await native.core.handle(event)
    assert result["action"]["error"] == "qq_file_not_offered" and worker.submits == 0


async def test_current_attachment_not_offered_to_another_conversation(native, monkeypatch):
    event, worker, ids, prepared, commands, _ = await setup(native, monkeypatch)
    other = ReplyScope("qq", "10001", "group", "99999")
    auth = ReplyAuthorizations(native.store).update(other, enabled=True, expected_version=None, actor="test")["version"]
    assert ReplyFiles(native.store).candidates(other, auth, "说明")["items"] == []


async def test_cancel_after_text_stops_all_files(native, monkeypatch):
    import asyncio
    import threading
    event, worker, ids, prepared, commands, _ = await setup(native, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    request = worker.request
    def blocking(command, **args):
        result = request(command, **args)
        if command == "submit_text":
            entered.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(worker, "request", blocking)
    task = asyncio.create_task(native.core.handle(event))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.submits == 1 and not prepared
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
