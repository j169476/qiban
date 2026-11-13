import secrets
from dataclasses import asdict

import pytest

from companion_core.errors import AssistantError
from companion_core.file_library import FileLibrary, describe, verify_files
from companion_core.media_originals import MediaOriginals
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


def grant(store, target="90001", account="80001", kind="group"):
    scope = ReplyScope("qq", account, kind, target)
    auth = ReplyAuthorizations(store)
    auth.update(scope, enabled=True, expected_version=None, actor="test")
    return scope, auth.version(scope)


def enabled(library, scope, content=b"test-original"):
    item = library.import_bytes(content, "使用说明.txt", ["说明"], actor="test")["item"]
    return library.configure(item["id"], item["name"], item["tags"], True,
                             [asdict(scope)], item["revision"], actor="test")


def test_encrypted_original_dedup_keeps_grants_and_filename(desktop_store):
    scope, version = grant(desktop_store)
    library = FileLibrary(desktop_store)
    content = "原件：中文与表情🙂".encode()
    item = enabled(library, scope, content)
    assert library.read_for_send(scope, version, item["id"], item["revision"], guard=lambda: None) == content
    duplicate = library.import_bytes(content, "别的名字.pdf", [], actor="test")
    assert duplicate == {"item": item, "duplicate": True}
    with desktop_store.db() as db:
        row = db.execute("SELECT * FROM file_library").fetchone()
        assert bytes(row["body"]) != content and "使用说明" not in row["metadata"]
    assert library.candidates(scope, version, "说明")[0]["id"] == item["id"]
    verify_files(desktop_store)


@pytest.mark.parametrize("target,account,kind", [("90002", "80001", "group"), ("90001", "80002", "group"), ("90001", "80001", "private")])
def test_other_conversation_account_and_kind_cannot_see_or_read(desktop_store, target, account, kind):
    scope, version = grant(desktop_store)
    other, other_version = grant(desktop_store, target, account, kind)
    library = FileLibrary(desktop_store)
    item = enabled(library, scope)
    assert library.candidates(other, other_version, "说明") == []
    with pytest.raises(AssistantError, match="当前会话"):
        library.read_for_send(other, other_version, item["id"], item["revision"], guard=lambda: None)


def test_revocation_disable_revision_and_cancel_stop_access(desktop_store):
    scope, version = grant(desktop_store)
    library = FileLibrary(desktop_store)
    item = enabled(library, scope)
    changed = library.configure(item["id"], "更名.txt", [], True, [asdict(scope)], item["revision"], actor="test")
    with pytest.raises(AssistantError) as error:
        library.read_for_send(scope, version, item["id"], item["revision"], guard=lambda: None)
    assert error.value.code == "file_library_changed"
    def cancelled():
        raise AssistantError("cancelled", "已取消")
    with pytest.raises(AssistantError) as error:
        library.read_for_send(scope, version, item["id"], changed["revision"], guard=cancelled)
    assert error.value.code == "cancelled"
    disabled = library.configure(item["id"], changed["name"], [], False, [], changed["revision"], actor="test")
    assert library.candidates(scope, version, "说明") == []
    with pytest.raises(AssistantError) as error:
        library.read_for_send(scope, version, item["id"], disabled["revision"], guard=lambda: None)
    assert error.value.code == "file_library_disabled"
    item = library.configure(item["id"], changed["name"], [], True, [asdict(scope)], disabled["revision"], actor="test")
    ReplyAuthorizations(desktop_store).update(scope, enabled=False, expected_version=version, actor="test")
    with pytest.raises(AssistantError) as error:
        library.read_for_send(scope, version, item["id"], item["revision"], guard=lambda: None)
    assert error.value.code == "reply_not_authorized"
    assert library.targets()["items"] == []


def test_import_inactive_and_unapproved_scope_cannot_be_enabled(desktop_store):
    library = FileLibrary(desktop_store)
    item = library.import_bytes(b"original", "文件.txt", [], actor="test")["item"]
    assert not item["enabled"] and not item["scopes"]
    scope = ReplyScope("qq", "80001", "group", "90001")
    for allowed in ([], [asdict(scope)]):
        with pytest.raises(AssistantError):
            library.configure(item["id"], item["name"], [], True, allowed, item["revision"], actor="test")
    assert library.get(item["id"]) == item


def test_shared_capacity_is_protected_and_full_import_rolls_back(desktop_store):
    library, media = FileLibrary(desktop_store), MediaOriginals(desktop_store)
    settings = media.capacity_settings()
    media.configure_capacity(1, settings["revision"], actor="test")
    library.import_bytes(b"x" * (1024 * 1024), "资料.bin", [], actor="test")
    assert media.capacity()["protected_bytes"] == 1024 * 1024
    media.cleanup()
    with pytest.raises(AssistantError) as error:
        library.import_bytes(b"new", "新资料.txt", [], actor="test")
    assert error.value.code == "media_capacity_full"
    assert len(library.listing()["items"]) == 1


def test_backup_restore_original_grants_and_tampered_archive_rolls_back(desktop_store):
    from companion_core import postgres_transfer
    scope, version = grant(desktop_store)
    library = FileLibrary(desktop_store)
    item = enabled(library, scope)
    key = secrets.token_bytes(32)
    archive = postgres_transfer.create(desktop_store, key)
    library.configure(item["id"], "改名.txt", [], False, [], item["revision"], actor="test")
    postgres_transfer.restore(desktop_store, archive, key, expected=postgres_transfer.fingerprint(desktop_store))
    assert library.get(item["id"]) == item
    auth = ReplyAuthorizations(desktop_store)
    with pytest.raises(AssistantError):
        library.read_for_send(scope, version, item["id"], item["revision"], guard=lambda: None)
    auth.update(scope, enabled=True, expected_version=auth.record(scope)["version"], actor="test")
    version = auth.version(scope)
    assert library.read_for_send(scope, version, item["id"], item["revision"], guard=lambda: None) == b"test-original"
    with desktop_store.db() as db:
        db.execute("UPDATE file_library SET body=? WHERE id=?", (b"damaged", item["id"]))
    corrupted = postgres_transfer.create(desktop_store, key)
    postgres_transfer.restore(desktop_store, archive, key, expected=postgres_transfer.fingerprint(desktop_store))
    before = postgres_transfer.fingerprint(desktop_store)
    with pytest.raises(AssistantError):
        postgres_transfer.restore(desktop_store, corrupted, key, expected=before)
    assert postgres_transfer.fingerprint(desktop_store) == before
    verify_files(desktop_store)


def test_management_import_idempotency_and_lock(service, tmp_path):
    path = tmp_path / "使用说明.txt"
    path.write_text("测试资料", encoding="utf-8")
    args = {"path": str(path), "tags": ["说明"]}
    request = secrets.token_hex(16)
    result = call(service, "files.import_file", args, request_id=request)
    assert result["ok"]
    assert call(service, "files.import_file", args, request_id=request) == result
    assert call(service, "files.list", {"after": "", "limit": 5})["result"]["items"] == [result["result"]["item"]]
    service.enable_lock("test-password-123456")
    service.lock()
    for operation, arguments in [("files.list", {"after": "", "limit": 5}), ("files.targets", {}), ("files.import_file", args)]:
        assert call(service, operation, arguments)["error"] == "desktop_locked"


@pytest.mark.parametrize("name", ["../secret.txt", "C:\\secret.txt", "CON.txt", "trailing.", "nul", "line\n.txt", "a/b.txt"])
def test_invalid_filename_cannot_become_native_path(name):
    with pytest.raises(AssistantError):
        describe(name, [])


def test_file_reply_limits_management_cas_persistence_and_independent_budget(service):
    from companion_core.media_budget import MediaBudgetSettings
    from companion_qq.file_replies import FileReplySettings
    incoming = MediaBudgetSettings(service.store).read()
    value = call(service, "files.settings")["result"]
    assert value == {"count": 3, "total_mib": 20, "revision": None}
    args = {"count": 4, "total_mib": 30, "expected": None}
    request = secrets.token_hex(16)
    saved = call(service, "files.settings.configure", args, request_id=request)
    assert saved["ok"]
    assert call(service, "files.settings.configure", args, request_id=request) == saved
    assert call(service, "files.settings.configure", args)["error"] == "edit_conflict"
    assert FileReplySettings(service.store).read() == saved["result"]
    assert MediaBudgetSettings(service.store).read() == incoming
    for count, total in [(0, 20), (13, 20), (3, 0), (3, 121), (True, 20)]:
        assert call(service, "files.settings.configure", {"count": count, "total_mib": total,
            "expected": saved["result"]["revision"]})["error"] == "qq_file_limits"
    service.enable_lock("test-password-123456")
    service.lock()
    assert call(service, "files.settings")["error"] == "desktop_locked"
