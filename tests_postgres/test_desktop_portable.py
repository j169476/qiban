"""Portable export/preview/hand-off with synthetic files and one disposable test database."""
import hashlib
import json
import secrets
import zlib

import pytest

from companion_core import postgres_transfer
from companion_core.cipher import ContentCipher
from companion_core.errors import AssistantError
from companion_service import recovery
from qiban import desktop_portable
from qiban.desktop_portable import DesktopPortable
from tests_postgres.test_desktop_management import call, desktop_store, service

__all__ = ["desktop_store", "service"]

PASSWORD = "portable-strong-password"
LOCK_PASSWORD = "temporary-strong-pass"
SUCCESS = {"switched": True, "restart_required": True, "restored": True}


class Stager:
    """Records the injected switch request; never performs a switch itself."""

    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result, error

    def __call__(self, **arguments):
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return dict(self.result or {})


def portable(service, stager=None, clock=None):
    arguments = {"key_provider": lambda: service.store._test_key, "stager": stager or Stager()}
    if clock is not None:
        arguments["clock"] = clock
    return DesktopPortable(service, **arguments)


def exported(portable_class, directory):
    target = directory / "kit.ccbackup"
    result = portable_class.export(str(target), PASSWORD)
    return target, directory / result["key_file"], result


def test_export_writes_an_authenticated_pair_and_never_overwrites(service, tmp_path):
    portable_class = portable(service)
    target, key_file, result = exported(portable_class, tmp_path)
    assert result["backup"] == "kit.ccbackup" and result["key_file"] == "kit.cckey"
    assert set(result) == {"backup", "key_file", "backup_sha256", "key_sha256", "backup_bytes",
                          "key_bytes", "records", "history", "encrypted"}
    assert target.is_file() and key_file.is_file()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == result["backup_sha256"]
    assert hashlib.sha256(key_file.read_bytes()).hexdigest() == result["key_sha256"]
    assert recovery.unwrap(key_file.read_bytes(), PASSWORD) == service.store._test_key
    assert postgres_transfer.unpack(target.read_bytes(), service.store._backup_key)["format"] == 1
    before = (target.read_bytes(), key_file.read_bytes())
    with pytest.raises(AssistantError) as refused:
        portable_class.export(str(target), PASSWORD)
    assert refused.value.code == "portable_exists"
    assert (target.read_bytes(), key_file.read_bytes()) == before
    # The independent recovery password is validated before any file is created.
    short = tmp_path / "short.ccbackup"
    with pytest.raises(AssistantError) as refused:
        portable_class.export(str(short), "too-short")
    assert refused.value.code == "recovery_password"
    assert not short.exists() and not (tmp_path / "short.cckey").exists()


def test_publication_race_never_overwrites_the_competing_file(service, tmp_path, monkeypatch):
    real_publish = desktop_portable._publish
    won = []

    def competing(temporary, target):
        if target.suffix == desktop_portable.KEY_SUFFIX and not won:
            won.append(target)
            target.write_bytes(b"competing key file")
        return real_publish(temporary, target)

    monkeypatch.setattr(desktop_portable, "_publish", competing)
    portable_class = portable(service)
    target = tmp_path / "race.ccbackup"
    with pytest.raises(AssistantError) as refused:
        portable_class.export(str(target), PASSWORD)
    assert refused.value.code == "portable_exists"
    assert won and won[0].read_bytes() == b"competing key file"
    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_export_rejects_a_published_file_that_no_longer_matches(service, tmp_path, monkeypatch):
    real_publish = desktop_portable._publish

    def corrupting(temporary, target):
        real_publish(temporary, target)
        with target.open("ab") as stream:
            stream.write(b"x")

    monkeypatch.setattr(desktop_portable, "_publish", corrupting)
    portable_class = portable(service)
    target = tmp_path / "corrupt.ccbackup"
    with pytest.raises(AssistantError) as refused:
        portable_class.export(str(target), PASSWORD)
    assert refused.value.code == "portable_write"
    assert "已删除" in str(refused.value)
    assert not target.exists() and not (tmp_path / "corrupt.cckey").exists()


def test_preview_authenticates_the_pair_without_writing_data(service, tmp_path):
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    fingerprint = postgres_transfer.fingerprint(service.store)
    manifest = service.store.read("manifest")["version"]
    result = portable_class.preview(str(target), str(key_file), PASSWORD)
    assert result["token"] and result["expires_in_seconds"] == 600
    assert result["preservation"]["name"].endswith(".ccbackup")
    assert result["records"] >= 0 and result["counters"]["ciphertexts"] >= 0
    assert "替换" in result["effect"] and "不合并" in result["effect"]
    assert "停止" in result["effect"] and "管理" in result["effect"]
    assert result["source"]["project"] == service.store.read("manifest")["data"]["project_id"][:12]
    assert result["source"]["role"] == service.store.read("persona")["data"]["name"]
    assert result["source"]["installation"] == service.store.state("desktop-owner")["installation"][:12]
    assert postgres_transfer.fingerprint(service.store) == fingerprint
    assert service.store.read("manifest")["version"] == manifest
    backup = service.store.root / "history" / "portable-preservation" / result["preservation"]["name"]
    assert backup.is_file()


@pytest.mark.parametrize("case", ["password", "pair", "truncated", "tampered_backup", "tampered_key"])
def test_preview_rejects_broken_pairs(service, tmp_path, case):
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    backup, key = str(target), str(key_file)
    password, expected = PASSWORD, "backup_invalid"
    if case == "password":
        password, expected = "wrong-portable-password", "recovery_key"
    elif case == "pair":
        # A key file that authenticates but holds another data key cannot open this backup.
        foreign = tmp_path / "foreign.cckey"
        foreign.write_bytes(recovery.wrap(secrets.token_bytes(32), PASSWORD))
        key = str(foreign)
    elif case == "truncated":
        broken = tmp_path / "truncated.ccbackup"
        broken.write_bytes(target.read_bytes()[:-8])
        backup = str(broken)
    elif case == "tampered_backup":
        broken = tmp_path / "tampered.ccbackup"
        data = bytearray(target.read_bytes())
        data[-1] ^= 1
        broken.write_bytes(bytes(data))
        backup = str(broken)
    else:
        broken = tmp_path / "tampered.cckey"
        data = bytearray(key_file.read_bytes())
        data[-1] ^= 1
        broken.write_bytes(bytes(data))
        key, expected = str(broken), "recovery_key"
    with pytest.raises(AssistantError) as refused:
        portable_class.preview(backup, key, password)
    assert refused.value.code == expected


def test_preview_rejects_missing_directory_and_oversized_files(service, tmp_path):
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    with pytest.raises(AssistantError) as refused:
        portable_class.preview(str(tmp_path / "missing.ccbackup"), str(key_file), PASSWORD)
    assert refused.value.code == "portable_missing"
    with pytest.raises(AssistantError) as refused:
        portable_class.preview(str(target), str(tmp_path), PASSWORD)
    assert refused.value.code == "portable_path"
    oversized = tmp_path / "oversized.cckey"
    oversized.write_bytes(b"x" * 301)
    with pytest.raises(AssistantError) as refused:
        portable_class.preview(str(target), str(oversized), PASSWORD)
    assert refused.value.code == "portable_limit"


def test_preview_rejects_a_snapshot_from_another_schema_version(service, tmp_path):
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    backup_key = portable_class._backup_key(service.store._test_key)
    snapshot = postgres_transfer.unpack(target.read_bytes(), backup_key)
    snapshot["migrations"] = [[999, "0" * 64]]
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    identifier = secrets.token_hex(16)
    legacy = tmp_path / "legacy.ccbackup"
    legacy.write_bytes(b"CCBACKUP1:" + identifier.encode() + b"\n"
                       + ContentCipher(backup_key).encrypt(zlib.compress(raw, 6), "backup", identifier, "1"))
    with pytest.raises(AssistantError) as refused:
        portable_class.preview(str(legacy), str(key_file), PASSWORD)
    assert refused.value.code == "portable_schema"


def test_publish_cleanup_failure_still_removes_the_published_target(service, tmp_path, monkeypatch):
    def refusing(temporary):
        raise OSError("temporary cleanup failed")

    monkeypatch.setattr(desktop_portable, "_discard", refusing)
    portable_class = portable(service)
    target = tmp_path / "discard.ccbackup"
    with pytest.raises(AssistantError) as refused:
        portable_class.export(str(target), PASSWORD)
    assert refused.value.code == "portable_write"
    assert not target.exists() and not (tmp_path / "discard.cckey").exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_preservation_never_prunes_existing_backups(service, tmp_path):
    policy = service.store.read("policy")
    service.store.write("policy", {**policy["data"], "backup_keep": 1}, policy["version"], "portable-test")
    backups = service.store.root / "history" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    old = backups / "2020-01-01-0000.ccbackup"
    old.write_bytes(b"older local backup")
    folder = service.store.root / "history" / "portable-preservation"
    folder.mkdir(parents=True, exist_ok=True)
    earlier = folder / ("0" * 32 + ".ccbackup")
    earlier.write_bytes(b"earlier preservation")
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    preserved = folder / preview["preservation"]["name"]
    assert preserved.is_file()
    assert old.read_bytes() == b"older local backup"
    assert earlier.read_bytes() == b"earlier preservation"
    assert sorted(path.name for path in backups.glob("*.ccbackup")) == [old.name]
    # Cancel keeps the preservation artifact; nothing here deletes earlier files.
    portable_class.cancel()
    assert preserved.is_file()


def test_dispatch_messages_never_include_paths_or_key_material(service, tmp_path):
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    answer = portable_class.dispatch({"action": "desktop-portable", "operation": "preview",
                                      "arguments": {"backup": str(target), "key_file": str(key_file),
                                                    "password": "wrong-portable-password"}})
    assert answer["ok"] is False and answer["error"] == "recovery_key"
    assert answer["message"] == "恢复口令或密钥文件不匹配，请重新核对"
    for secret in (PASSWORD, "wrong-portable-password", str(tmp_path), service.store._test_key.hex()):
        assert secret not in answer["message"]


def test_preview_requires_a_stopped_role_and_no_pending_restore(service, tmp_path):
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    service.store.set_state("core-run:local", {"status": "active"})
    with pytest.raises(AssistantError) as refused:
        portable_class.preview(str(target), str(key_file), PASSWORD)
    assert refused.value.code == "restore_active"
    service.store.set_state("core-run:local", {"status": "paused"})
    service.store.set_state("desktop-last-restore", {"status": "assets_pending"})
    with pytest.raises(AssistantError) as refused:
        portable_class.preview(str(target), str(key_file), PASSWORD)
    assert refused.value.code == "restore_pending"


def test_begin_and_complete_hand_the_pair_to_the_stager_once(service, tmp_path):
    stager = Stager(result=SUCCESS)
    portable_class = portable(service, stager)
    target, key_file, _ = exported(portable_class, tmp_path)
    fingerprint = postgres_transfer.fingerprint(service.store)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    challenge = portable_class.begin(preview["token"])["challenge"]
    report = portable_class.complete(challenge)
    assert len(stager.calls) == 1
    call_arguments = stager.calls[0]
    # The hand-off carries the trusted source name the preview kept for the authenticated
    # import: its own backup file name, not a path and not anything the page sent.
    assert set(call_arguments) == {"source_blob", "source_key", "expected_current_fingerprint",
                                   "preservation", "source_name"}
    assert call_arguments["source_name"] == target.name == "kit.ccbackup"
    assert call_arguments["source_blob"] == target.read_bytes()
    assert call_arguments["source_key"] == service.store._test_key
    assert call_arguments["expected_current_fingerprint"] == fingerprint
    assert call_arguments["preservation"]["name"].endswith(".ccbackup")
    # The import that was restored and this machine's own pre-restore copy are two files.
    assert call_arguments["preservation"]["name"] != call_arguments["source_name"]
    assert report == {"stager": SUCCESS, "switched": True, "restored": True,
                      "restart_required": True, "preservation": call_arguments["preservation"]}
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == "portable_verification_expired"
    assert len(stager.calls) == 1


def test_cancel_discards_the_preview_and_never_calls_the_stager(service, tmp_path):
    stager = Stager(result=SUCCESS)
    portable_class = portable(service, stager)
    target, key_file, _ = exported(portable_class, tmp_path)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    assert portable_class.cancel() == {"cancelled": True}
    assert portable_class.preview_state is None and portable_class.source_key is None
    with pytest.raises(AssistantError) as refused:
        portable_class.begin(preview["token"])
    assert refused.value.code == "portable_preview_expired"
    assert stager.calls == []
    backup = service.store.root / "history" / "portable-preservation" / preview["preservation"]["name"]
    assert backup.is_file()


def test_stager_failure_is_reported_uncertain_and_never_retried(service, tmp_path):
    stager = Stager(error=RuntimeError("switch host failed"))
    portable_class = portable(service, stager)
    target, key_file, _ = exported(portable_class, tmp_path)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    challenge = portable_class.begin(preview["token"])["challenge"]
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == "portable_switch_uncertain"
    assert "不要重复提交" in str(refused.value) and "未变" not in str(refused.value)
    assert portable_class.source_key is None
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == "portable_verification_expired"
    assert len(stager.calls) == 1


@pytest.mark.parametrize("result", [None, {}, {"switched": True}, {"switched": True, "restored": True},
                                    {"switched": True, "restored": False, "restart_required": True},
                                    {"switched": True, "restored": True, "restart_required": "yes"},
                                    "ok"])
def test_incomplete_stager_report_is_uncertain(service, tmp_path, result):
    stager = Stager(result=result)
    portable_class = portable(service, stager)
    target, key_file, _ = exported(portable_class, tmp_path)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    challenge = portable_class.begin(preview["token"])["challenge"]
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == "portable_switch_uncertain"
    assert len(stager.calls) == 1


def test_changed_files_or_database_are_refused_before_the_stager(service, tmp_path):
    stager = Stager(result=SUCCESS)
    portable_class = portable(service, stager)
    target, key_file, _ = exported(portable_class, tmp_path)
    original = target.read_bytes()
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    challenge = portable_class.begin(preview["token"])["challenge"]
    target.write_bytes(original[:-1])
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == "portable_changed"
    assert stager.calls == []
    target.write_bytes(original)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    challenge = portable_class.begin(preview["token"])["challenge"]
    service.store.set_state("portable-db-change", {"revision": 1})
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == "portable_changed"
    assert stager.calls == []


def test_lock_and_reopened_session_destroy_cached_portable_state(service, tmp_path):
    call(service, "lock.enable", {"password": LOCK_PASSWORD})
    portable_class = portable(service)
    target, key_file, _ = exported(portable_class, tmp_path)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    assert portable_class.source_key is not None and portable_class.preview_state is not None
    assert call(service, "session.lock")["ok"]
    with pytest.raises(AssistantError) as refused:
        portable_class.begin(preview["token"])
    assert refused.value.code == "desktop_locked"
    assert portable_class.source_key is None and portable_class.preview_state is None
    with pytest.raises(AssistantError) as refused:
        portable_class.export(str(tmp_path / "locked.ccbackup"), PASSWORD)
    assert refused.value.code == "desktop_locked"
    call(service, "session.unlock", {"password": LOCK_PASSWORD})
    with pytest.raises(AssistantError) as refused:
        portable_class.complete("never-issued")
    assert refused.value.code == "portable_verification_expired"


def test_expired_preview_and_challenge_are_refused(service, tmp_path):
    now = [1000.0]
    stager = Stager(result=SUCCESS)
    portable_class = portable(service, stager, clock=lambda: now[0])
    target, key_file, _ = exported(portable_class, tmp_path)
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    now[0] += 601
    with pytest.raises(AssistantError) as refused:
        portable_class.begin(preview["token"])
    assert refused.value.code == "portable_preview_expired"
    preview = portable_class.preview(str(target), str(key_file), PASSWORD)
    challenge = portable_class.begin(preview["token"])["challenge"]
    now[0] += 181
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == "portable_verification_expired"
    assert stager.calls == []


@pytest.mark.parametrize("message, expected", [
    ({"action": "desktop-portable", "operation": "preview", "arguments": {"backup": "only"}},
     "management_request"),
    ({"action": "management", "operation": "cancel", "arguments": {}}, "management_request"),
    ({"action": "desktop-portable", "operation": "switch", "arguments": {}}, "management_request"),
    ({"action": "desktop-portable", "operation": "begin", "arguments": {"token": "never-issued"}},
     "portable_preview_expired"),
])
def test_dispatch_envelope_is_strict_and_messages_are_fixed(service, message, expected):
    portable_class = portable(service)
    answer = portable_class.dispatch(message)
    assert answer["ok"] is False and answer["error"] == expected
    assert set(answer) == {"ok", "error", "message"}
    assert PASSWORD not in answer["message"]
    assert portable_class.dispatch({"action": "desktop-portable", "operation": "cancel",
                                    "arguments": {}}) == {"ok": True, "result": {"cancelled": True}}
