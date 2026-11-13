import io
import secrets

import pytest
from PIL import Image

from companion_core.errors import AssistantError
from companion_core.media_originals import MediaOriginals
from companion_core.sticker_library import StickerLibrary, verify_stickers
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


def picture(animated=False):
    buffer = io.BytesIO()
    first = Image.new("RGB", (24, 20), "red")
    if animated:
        first.save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", (24, 20), "blue")],
            duration=100,
            loop=0,
        )
    else:
        first.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("animated", [False, True])
def test_import_deduplicate_preserve_original_preview_and_encryption(desktop_store, animated):
    lib = StickerLibrary(desktop_store)
    content = picture(animated)
    result = lib.import_bytes(content, "打招呼", ["你好", "开心"], actor="test")
    item = result["item"]
    assert not item["enabled"] and item["frames"] == (2 if animated else 1)
    assert item["preview"].startswith("data:image/png;base64,")
    assert lib.candidates("你好") == []
    saved = lib.configure(item["id"], item["name"], item["tags"], True, item["revision"], actor="test")
    assert lib.read_for_send(item["id"], saved["revision"], guard=lambda: None) == content
    duplicate = lib.import_bytes(content, "不可覆盖", ["不能覆盖"], actor="test")
    assert duplicate["duplicate"] and duplicate["item"] == saved
    with desktop_store.db() as db:
        row = db.execute("SELECT metadata,body FROM sticker_library WHERE id=?", (item["id"],)).fetchone()
    assert row["metadata"].startswith("enc1:") and "打招呼" not in row["metadata"]
    assert bytes(row["body"]) != content
    verify_stickers(desktop_store)


def test_repeated_selection_has_no_usage_or_time_gate_but_disable_and_revision_apply(desktop_store):
    lib = StickerLibrary(desktop_store)
    item = lib.import_bytes(picture(), "你好", ["问候"], actor="test")["item"]
    item = lib.configure(item["id"], item["name"], item["tags"], True, item["revision"], actor="test")
    for _ in range(12):
        assert lib.candidates("问候")[0]["id"] == item["id"]
        assert lib.read_for_send(item["id"], item["revision"], guard=lambda: None) == picture()
    updated = lib.configure(item["id"], "新名字", ["问候"], True, item["revision"], actor="test")
    with pytest.raises(AssistantError) as error:
        lib.read_for_send(item["id"], item["revision"], guard=lambda: None)
    assert error.value.code == "sticker_changed"
    lib.configure(item["id"], updated["name"], updated["tags"], False, updated["revision"], actor="test")
    assert lib.candidates("问候") == []
    with pytest.raises(AssistantError) as error:
        lib.read_for_send(item["id"], updated["revision"], guard=lambda: None)
    assert error.value.code == "sticker_disabled"


def test_shared_capacity_protects_stickers_from_retention(desktop_store):
    lib, media = StickerLibrary(desktop_store), MediaOriginals(desktop_store)
    lib.import_bytes(picture(), "保留", [], actor="test")
    capacity = media.capacity()
    assert capacity["used_bytes"] == capacity["protected_bytes"] == len(picture())
    media.cleanup(guard=lambda: None)
    assert len(lib.listing()["items"]) == 1


def test_authenticated_management_import_dedup_edit_conflict_and_lock(service, tmp_path):
    path = tmp_path / "测试图片.png"
    path.write_bytes(picture())
    request = secrets.token_hex(16)
    args = {"path": str(path), "name": "快乐", "tags": ["开心"]}
    imported = call(service, "stickers.import_file", args, request_id=request)
    assert imported["ok"]
    assert call(service, "stickers.import_file", args, request_id=request) == imported
    item = imported["result"]["item"]
    edit = {
        "identifier": item["id"],
        "name": "快乐",
        "tags": ["开心"],
        "enabled": True,
        "expected": item["revision"],
    }
    assert call(service, "stickers.configure", edit)["ok"]
    assert call(service, "stickers.configure", edit)["error"] == "edit_conflict"
    service.enable_lock("test-password-123456")
    service.lock()
    assert call(service, "stickers.list", {"after": "", "limit": 5})["error"] == "desktop_locked"
    assert call(service, "stickers.import_file", args)["error"] == "desktop_locked"


def test_backup_restores_sticker_original_and_metadata(desktop_store):
    from companion_core import postgres_transfer

    lib = StickerLibrary(desktop_store)
    item = lib.import_bytes(picture(True), "动图", ["开心"], actor="test")["item"]
    key = secrets.token_bytes(32)
    archive = postgres_transfer.create(desktop_store, key)
    lib.configure(item["id"], "修改后", [], True, item["revision"], actor="test")
    postgres_transfer.restore(
        desktop_store, archive, key, expected=postgres_transfer.fingerprint(desktop_store)
    )
    assert lib.get(item["id"]) == item
    verify_stickers(desktop_store)


def test_corrupt_image_is_rejected_without_record(desktop_store):
    lib = StickerLibrary(desktop_store)
    with pytest.raises(AssistantError):
        lib.import_bytes(b"not an image", "无效图片", [], actor="test")
    assert lib.listing()["items"] == []
