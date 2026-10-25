from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.postgres import migrate


def test_encrypted_content_cas_and_history(pg_store):
    store = pg_store
    original = store.read("policy")
    changed = deepcopy(original["data"])
    changed["worker_timeout_seconds"] = 90
    result = store.write("policy", changed, original["version"], "test")
    assert store.read("policy") == result
    with pytest.raises(AssistantError) as exc:
        store.write("policy", original["data"], original["version"], "stale")
    assert exc.value.code == "edit_conflict"
    with store.db() as db:
        row = db.execute("SELECT body,revision FROM content_records WHERE kind='policy'").fetchone()
        assert b"worker_timeout_seconds" not in bytes(row["body"])
        assert row["revision"] == 2
        assert db.execute("SELECT count(*) FROM content_history WHERE kind='policy'").fetchone()[0] == 1
    assert not (store.root / "policy.yaml").exists()


def test_ledger_and_content_rollback_together(pg_store):
    original = pg_store.read("policy")
    with pytest.raises(RuntimeError):
        with pg_store.db():
            pg_store.set_state("atomic-receipt", True)
            pg_store.write("policy", {**original["data"], "worker_timeout_seconds": 95}, original["version"], "test")
            raise RuntimeError("synthetic interruption")
    assert pg_store.state("atomic-receipt") is None
    assert pg_store.read("policy") == original


def test_concurrent_edits_have_one_winner(pg_store):
    original = pg_store.read("policy")

    def update(value):
        try:
            pg_store.write("policy", {**original["data"], "worker_timeout_seconds": value}, original["version"], "test")
            return "committed"
        except AssistantError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (90, 100)))
    assert sorted(results) == ["committed", "edit_conflict"]


def test_migration_is_idempotent_and_checks_checksum(pg_store):
    migrate(pg_store.database, pg_store.profile)
    with pg_store.db() as db:
        db.execute("UPDATE schema_migrations SET checksum='tampered'")
    with pytest.raises(AssistantError) as exc:
        migrate(pg_store.database, pg_store.profile)
    assert exc.value.code == "migration_checksum"


def test_metadata_repository_works_with_postgres(pg_store):
    repository = GovernanceRepository(pg_store)
    repository.initialize()
    assert isinstance(repository.epoch(), int)
    repository.reset_after_restore()
    assert not repository.session_active("not-a-session")


def test_ciphertext_tampering_is_not_ignored(pg_store):
    with pg_store.db() as db:
        db.execute("UPDATE content_records SET body=? WHERE kind='policy'", (b"tampered",))
    with pytest.raises(AssistantError) as exc:
        pg_store.read("policy")
    assert exc.value.code == "storage_integrity"


def test_memory_cache_is_revision_ciphertext_bound_and_never_shared_mutably(pg_store):
    from companion_core.errors import AssistantError

    first = pg_store.read("companion")
    first["data"]["schema_version"] = 999
    assert pg_store.read("companion")["data"]["schema_version"] == 1
    with pg_store.db() as db:
        db.execute("UPDATE content_records SET body=? WHERE kind='companion'", (b"tampered",))
    with pytest.raises(AssistantError):
        pg_store.read("companion")
