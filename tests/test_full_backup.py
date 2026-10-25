import zipfile

import pytest

from bili_assistant.api import call
from bili_assistant.backups import MANIFEST, create, restore
from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError
from bili_assistant.storage import Store


def test_full_restore_preserves_old_receipts_quotas_assets_and_pauses(role, tmp_path):
    from test_persona import configure

    configure(role.store, visual=True)
    for i in range(105):
        role.relationship("789", "owner", 0, f"event-{i}")
    role.store.set_state("model-cycles:test", 12)
    role.store.set_state("health:old", {"phase": "active"})
    role.store.set_state("fault:old", {"id": "old", "notification": "queued"})
    with role.store.db() as db:
        for n, state in enumerate(("succeeded", "prepared", "dispatching", "uncertain")):
            db.execute("""INSERT INTO actions
                (id,dedupe,owner,generation,account,kind,payload,status,units,created)
                VALUES (?,?, 'role','old','123','reply','{}',?,1,'2026-10-20')""", (str(n), str(n), state))
    backup = create(role.store, tmp_path / "full.zip")
    destination = tmp_path / "restored"
    result = restore(backup["path"], destination)
    recovered_store = Store(destination)
    recovered = Engine(recovered_store, role.host, role.adapter, role.clock)
    assert result["status"] == "paused" and not result["activated"]
    assert recovered_store.state("run")["status"] == "paused"
    assert recovered_store.state("run")["generation"] != role.store.state("run")["generation"]
    assert recovered_store.state("model-cycles:test") == 12
    assert recovered_store.state("health:old") is None
    assert recovered_store.state("fault:old")["notification"] == "cancelled_on_restore"
    assert recovered.relationship("789", "owner", 1, "event-0")["duplicate"]
    assert recovered_store.read("relationships", "789")["data"]["interactions"] == 105
    assert (destination / "assets/persona/turnaround.png").exists()
    with recovered_store.db() as db:
        assert [row[0] for row in db.execute("SELECT status FROM actions ORDER BY id")] == [
            "succeeded", "cancelled", "uncertain", "uncertain"]
    assert role.store.state("run")["status"] == "active"
    with pytest.raises(AssistantError, match="新目录"):
        restore(backup["path"], destination)


@pytest.mark.parametrize("change", ["checksum", "missing", "traversal", "duplicate", "legacy"])
def test_restore_rejects_invalid_archives_without_publishing_target(role, tmp_path, change):
    original = tmp_path / "full.zip"
    create(role.store, original)
    with zipfile.ZipFile(original) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    if change == "checksum":
        entries["persona.yaml"] += b"\n# changed\n"
    elif change == "missing":
        del entries["runtime/state.sqlite3"]
    elif change == "traversal":
        entries["../outside.yaml"] = b"unsafe"
    elif change == "legacy":
        del entries[MANIFEST]
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if change == "duplicate":
            archive.writestr("PERSONA.yaml", entries["persona.yaml"])
    destination = tmp_path / "destination"
    with pytest.raises(AssistantError):
        restore(bad, destination)
    assert not destination.exists() and not (tmp_path / "outside.yaml").exists()


def test_restore_interruption_never_publishes_partial_target(role, tmp_path, monkeypatch):
    import bili_assistant.backups as module

    archive = tmp_path / "full.zip"
    create(role.store, archive)
    original = role.store.read("persona")["version"]
    monkeypatch.setattr(module, "pause_database", lambda _: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(AssistantError):
        restore(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert role.store.read("persona")["version"] == original


def test_backup_interruption_keeps_previous_complete_archive(role, tmp_path, monkeypatch):
    target = tmp_path / "full.zip"
    create(role.store, target)
    before = target.read_bytes()
    monkeypatch.setattr(role.store, "export", lambda _: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError):
        create(role.store, target)
    assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_restore_is_admin_only_and_requires_pause(role, admin, tmp_path):
    args = {"archive": str(tmp_path / "backup.zip"), "destination": str(tmp_path / "restored")}
    with pytest.raises(AssistantError, match="admin"):
        await call(role, "data_restore", args)
    with pytest.raises(AssistantError, match="暂停"):
        await call(admin, "data_restore", args)
    role.stop(paused=True)
    backup = await call(admin, "data_backup", {})
    args["archive"] = backup["path"]
    assert (await call(admin, "data_restore", args))["restored"]


@pytest.mark.parametrize("change", ["truncated", "same_stamp", "checksum", "database"])
def test_daily_backup_repairs_corrupt_existing_archive(role, tmp_path, change):
    import hashlib
    import json
    import os
    from pathlib import Path

    made = role.store.backup()
    target = Path(made["path"])
    if change == "truncated":
        target.write_bytes(b"not a zip")
    elif change == "same_stamp":
        previous = target.stat()
        target.write_bytes(b"x" * previous.st_size)
        os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    else:
        with zipfile.ZipFile(target) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        if change == "checksum":
            entries["persona.yaml"] += b"\n# invalid checksum\n"
        else:
            name = "runtime/state.sqlite3"
            entries[name] = b"broken sqlite but matching archive hash"
            manifest = json.loads(entries[MANIFEST])
            manifest["files"][name] = {"size": len(entries[name]), "sha256": hashlib.sha256(entries[name]).hexdigest()}
            entries[MANIFEST] = json.dumps(manifest).encode()
        with zipfile.ZipFile(target, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
    repaired = role.store.backup()
    assert repaired["repaired"] and repaired["verified"] and not repaired["reused"]
    assert restore(target, tmp_path / "repaired")["restored"]
    assert role.store.backup()["reused"]


def test_failed_backup_repair_keeps_old_archives_and_history(role, monkeypatch):
    from pathlib import Path

    import bili_assistant.backups as module

    original = role.store.backup()
    target = Path(original["path"])
    old = target.parent / "older.full.zip"
    old.write_bytes(target.read_bytes())
    snapshots = [role.store.root / "history" / f"test-{i:03}.yaml" for i in range(205)]
    for path in snapshots:
        path.write_text("history", encoding="utf-8")
    target.write_bytes(b"corrupt")
    monkeypatch.setattr(module, "create", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        role.store.checkpoint()
    assert old.exists() and all(path.exists() for path in snapshots)


def test_daily_backup_serializes_concurrent_creators(role):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: role.store.backup(), range(2)))
    assert all(result["verified"] for result in results)
    assert sum(result["reused"] for result in results) == 1


def test_retention_keeps_verified_current_archive_even_with_old_timestamp(role):
    import os
    from pathlib import Path

    policy = role.store.read("policy")
    role.store.write("policy", {**policy["data"], "backup_keep": 1}, policy["version"], "test")
    target = Path(role.store.backup()["path"])
    os.utime(target, ns=(1, 1))
    other = target.parent / "newer.full.zip"
    other.write_bytes(target.read_bytes())
    result = role.store.backup()
    assert result["verified"] and result["reused"]
    assert target.exists() and not other.exists()
