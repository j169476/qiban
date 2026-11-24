import secrets
from dataclasses import asdict

import pytest

from companion_core import backup_stream, postgres_transfer
from companion_core.errors import AssistantError
from companion_core.file_library import FileLibrary
from companion_core.library_versions import LibraryVersions, verify_versions
from companion_core.media_originals import MediaOriginals
from companion_core.sticker_library import StickerLibrary
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service
from tests_postgres.test_file_library import grant
from tests_postgres.test_sticker_library import picture


def test_edit_restore_bytes_and_names_without_reauthorizing(service, tmp_path):
    store = service.store
    library = FileLibrary(store)
    item = library.import_bytes(b"old", "报告.txt", ["旧"], actor="test")["item"]
    target = {"identifier": item["id"], "layer": "files"}
    original = item["revision"]
    scope, auth = grant(store)
    updated = library.configure(item["id"], "新报告.txt", ["新"], True, [asdict(scope)], original, actor="test")
    chosen = tmp_path / "chosen.txt"
    chosen.write_bytes(b"new")
    result = call(service, "library.replace", {**target, "expected": updated["revision"], "path": str(chosen)})
    assert result["ok"], result
    replaced = result["result"]
    assert library.read_for_send(scope, auth, item["id"], replaced["revision"], guard=lambda: None) == b"new"
    assert not call(service, "library.restore", {**target, "expected": original, "revision": original})["ok"]
    # A new import can now hold the old bytes under its own ID. Restoration must
    # not merge the items or re-enable scopes revoked since the revision.
    other = library.import_bytes(b"old", "另一份.txt", [], actor="test")["item"]
    assert other["id"] != item["id"]
    disabled = library.configure(item["id"], replaced["name"], [], False, [], replaced["revision"], actor="test")
    restored = call(service, "library.restore", {**target, "expected": disabled["revision"], "revision": original})
    assert restored["ok"], restored
    assert (restored["result"]["name"], restored["result"]["tags"]) == ("报告.txt", ["旧"])
    assert not restored["result"]["enabled"] and restored["result"]["scopes"] == []
    assert library.get(other["id"]) == other
    items = call(service, "library.versions", {**target, "after": None})["result"]["items"]
    assert items[0]["current"] and not any(r["current"] for r in items[1:])
    export = tmp_path / "historical.txt"
    assert call(service, "library.export", {**target, "revision": replaced["revision"], "path": str(export)})["ok"]
    assert export.read_bytes() == b"new"
    service.unlocked = False
    assert call(service, "library.versions", {**target, "after": None})["error"] == "desktop_locked"
    assert call(service, "library.export", {**target, "revision": original, "path": str(export)})["error"] == "desktop_locked"
    assert export.read_bytes() == b"new"


def test_sticker_history_backup_and_page_cursor(desktop_store, tmp_path):
    store, key = desktop_store, secrets.token_bytes(32)
    lib = StickerLibrary(store)
    old = lib.import_bytes(picture(), "表情", [], actor="test")["item"]
    versions = LibraryVersions(store, "stickers")
    original = old["revision"]
    with pytest.raises(AssistantError):
        versions.save(old["id"], original, actor="test", content=b"invalid image")
    item = versions.save(old["id"], original, actor="test", content=picture(True))
    changed = item["revision"]
    for i in range(7):
        item = lib.configure(item["id"], "表情" + str(i), [], False, item["revision"], actor="test")
    pages, after = [], None
    while True:
        page = versions.listing(item["id"], after)
        pages.extend(page["items"])
        after = page["after"]
        if after is None:
            break
    assert len({row["revision"] for row in pages}) == len(pages) == 9
    assert pages[-1]["revision"] == original and pages[-1]["preview"].startswith("data:image/png;base64,")
    copied = tmp_path / "copy.backup"
    saved = backup_stream.create(store, copied, key)
    restored = versions.save(item["id"], item["revision"], actor="test", revision=original)
    assert restored["format"] == "PNG" and restored["name"] == "表情"
    backup_stream.restore(store, copied, key, expected=backup_stream.fingerprint(store), digest=saved["sha256"])
    assert lib.get(item["id"]) == item
    assert versions.read_version(item["id"], original) == picture()
    assert versions.read_version(item["id"], changed) == picture(True)
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM library_version_bodies").fetchone()[0] == 1
    verify_versions(store)


def test_history_respects_protected_capacity_and_rolls_back(desktop_store):
    media = MediaOriginals(desktop_store)
    media.configure_capacity(1, None, actor="test")
    lib = FileLibrary(desktop_store)
    item = lib.import_bytes(b"X" * 700_000, "文件.bin", [], actor="test")["item"]
    versions = LibraryVersions(desktop_store, "files")
    with pytest.raises(AssistantError, match="空间不足"):
        versions.save(item["id"], item["revision"], actor="test", content=b"Y" * 700_000)
    assert lib.get(item["id"]) == item
    assert media.capacity()["protected_bytes"] == 700_000
    assert len(versions.listing(item["id"], None)["items"]) == 1


@pytest.mark.parametrize("old_schema", [16, 17, 18, 19, 20, 21, 22])
def test_old_backup_restores_original_as_first_editable_revision(desktop_store, tmp_path, old_schema):
    store, key = desktop_store, secrets.token_bytes(32)
    lib = FileLibrary(store)
    item = lib.import_bytes(b"old", "资料.txt", [], actor="test")["item"]
    def previous_records(db):
        newer = {t for v, fields in postgres_transfer.ADDITIVE_TABLES.items() if v > old_schema for t in fields}
        skip = False
        for entry in backup_stream.records(store, db):
            if entry[0] == "schema":
                yield ["schema", entry[1][:old_schema]]
            else:
                if entry[0] == "table":
                    skip = entry[1] in newer
                if not skip:
                    yield entry
    old_path = tmp_path / "previous.qbk"
    with old_path.open("wb") as writer, store.db() as db:
        backup_stream.write(writer, previous_records(db), key)
    blob = old_path.read_bytes()
    snapshot = postgres_transfer.unpack(blob, key)
    assert snapshot["migrations"][-1][0] == max(postgres_transfer.ADDITIVE_TABLES)
    assert snapshot["tables"]["role_reminders"]["rows"] == []
    assert snapshot["tables"]["library_versions"]["rows"] == []
    versions = LibraryVersions(store, "files")
    versions.save(item["id"], item["revision"], actor="test", content=b"new")
    backup_stream.restore(store, old_path, key, expected=backup_stream.fingerprint(store),
                          digest=backup_stream.inspect(old_path, key)["sha256"])
    assert lib.get(item["id"]) == item
    assert len(versions.listing(item["id"], None)["items"]) == 1
