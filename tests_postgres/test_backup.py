import hashlib
import io
import secrets

import pytest
from PIL import Image

from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.postgres_transfer import create, fingerprint, restore, unpack


def test_backup_restores_full_ledger_but_not_revoked_access(pg_store):
    store, key = pg_store, secrets.token_bytes(32)
    repo = GovernanceRepository(store)
    store.set_state("private-marker", "private-sensitive-canary")
    store.set_state("model-budget:2026-10-23", {"used": 9, "sources": {}})
    store.set_state("core-run", {"status": "paused", "generation": "old", "owner": "control"})
    epoch = repo.epoch()
    with store.db() as db:
        db.execute("INSERT INTO governance_sessions VALUES(?,?,?,?,?,?)", ("old-session", "owner", "reply", 9999999999, epoch, 0))
    blob = create(store, key)
    assert b"private-sensitive-canary" not in blob and b"old-session" not in blob
    store.set_state("private-marker", "changed")
    old = store.read("policy")
    store.write("policy", {**old["data"], "worker_timeout_seconds": 97}, old["version"], "test")
    result = restore(store, blob, key, expected=fingerprint(store))
    assert result["restored"] and not result["role_started"]
    assert store.state("private-marker") == "private-sensitive-canary"
    assert store.state("model-budget:2026-10-23")["used"] == 9
    assert store.read("policy") == old
    assert store.state("core-run")["status"] == "paused"
    assert repo.epoch() != epoch and not repo.session_active("old-session")


def test_tampered_backup_and_stale_preview_leave_data_unchanged(pg_store):
    key = secrets.token_bytes(32)
    blob = create(pg_store, key)
    original = fingerprint(pg_store)
    with pytest.raises(AssistantError):
        unpack(blob[:-1] + bytes([blob[-1] ^ 1]), key)
    with pytest.raises(AssistantError):
        unpack(blob, secrets.token_bytes(32))
    pg_store.set_state("new-receipt", True)
    with pytest.raises(AssistantError) as exc:
        restore(pg_store, blob, key, expected=original)
    assert exc.value.code == "edit_conflict" and pg_store.state("new-receipt") is True


def test_registered_image_restores_with_recoverable_failed_file_write(pg_store, monkeypatch):
    from companion_core import asset_transfer

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    raw = buffer.getvalue()
    path = pg_store.root / "assets/persona/reference.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    persona = pg_store.read("persona")
    persona["data"]["visual_identity"] = {"description": "synthetic image", "references": [
        {"filename": "reference.png", "sha256": hashlib.sha256(raw).hexdigest(), "label": "test"}]}
    pg_store.write("persona", persona["data"], persona["version"], "test")
    key = secrets.token_bytes(32)
    blob = create(pg_store, key)
    path.unlink()
    original = asset_transfer.atomic_write

    def fail(*args, **kwargs):
        raise OSError("synthetic file failure")

    monkeypatch.setattr(asset_transfer, "atomic_write", fail)
    with pytest.raises(OSError):
        restore(pg_store, blob, key, expected=fingerprint(pg_store))
    assert not pg_store.validate_critical()["ok"]
    monkeypatch.setattr(asset_transfer, "atomic_write", original)
    assert asset_transfer.finish(pg_store)["assets_restored"] == 1
    assert path.read_bytes() == raw and pg_store.validate_critical()["ok"]
