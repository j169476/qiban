"""Independent review checks for upgrade safety boundaries."""

import test_upgrade_cleanup_gate as fixtures
from test_upgrade_cleanup_gate import (
    INSTALLATION,
    OLDER_TRANSACTION,
    cleanup,
    data,
    generation_tree,
    record_at,
    registry,
)
from test_upgrade_program import OLD_FILES, published
from upgrade import gate

root = fixtures.root
local_acl = fixtures.local_acl


def test_corrupt_program_resource_cannot_supply_a_trusted_identity(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    published(app, OLD_FILES)
    (app / "resources/storage/worker.exe").write_bytes(b"partial installation")
    assert gate.running_identity(app / "resources/storage/worker.exe") is None


def test_candidate_without_its_own_verified_archive_cannot_be_dropped(root, monkeypatch):
    record_at(root, "confirmed")
    plan = data.plan_clone(INSTALLATION, OLDER_TRANSACTION)
    registry.register_generation(root, INSTALLATION, plan, OLDER_TRANSACTION)
    generation_tree(root, plan["generation"])
    dropped = []
    monkeypatch.setattr(cleanup.data, "drop_database", lambda *args: dropped.append(args[-1]))
    try:
        cleanup.clean(root, state_value={"installation": INSTALLATION}, credential={})
    except Exception:
        pass
    assert dropped == [], "another generation's valid archive cannot authorize this deletion"


def test_incomplete_removal_keeps_generation_in_registry(root, monkeypatch):
    record_at(root, "confirmed")
    plan = data.plan_clone(INSTALLATION, OLDER_TRANSACTION)
    registry.register_generation(root, INSTALLATION, plan, OLDER_TRANSACTION)
    generation_tree(root, plan["generation"])
    monkeypatch.setattr(cleanup.data, "drop_database", lambda *args: None)
    monkeypatch.setattr(cleanup.shutil, "rmtree", lambda *args, **kwargs: None)
    try:
        cleanup.clean(root, state_value={"installation": INSTALLATION}, credential={})
    except Exception:
        pass
    remaining = registry.load(root, INSTALLATION)["generations"]
    assert plan["generation"] in {item["generation"] for item in remaining}


def test_normal_open_does_not_rehash_the_program(root, monkeypatch):
    def forbidden(_):
        raise AssertionError("no program hashing outside an unfinished upgrade")
    monkeypatch.setattr(gate, "running_identity", forbidden)
    assert gate.identity_for_root(root, "unused.exe") is None


def test_worker_cannot_open_database_while_maintenance_owns_installation(root, monkeypatch, capsys):
    import json
    import sys

    import storage_worker
    from filelock import FileLock

    monkeypatch.setattr(sys, "argv", ["worker", "--config", str(root / "unused.json")])
    monkeypatch.setattr(storage_worker, "config_spec", lambda _: {"root": root})
    def forbidden(*args):
        raise AssertionError("database must not be opened while maintenance holds the lock")
    monkeypatch.setattr(storage_worker, "open_store", forbidden)
    with FileLock(str(root / "installation.lock"), timeout=0):
        assert storage_worker.main() == 1
    assert json.loads(capsys.readouterr().out)["error"] == "installation_busy"
