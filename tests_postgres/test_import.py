import secrets

import pytest

from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.legacy_import import apply, capture
from companion_core.postgres import PostgresStore
from companion_core.storage import Store


def test_legacy_migration_preserves_raw_versions_quota_and_pause(pg_store, tmp_path):
    legacy = Store(tmp_path / "legacy")
    legacy.initialize()
    legacy.set_state("core-model:2026-10-23", 17)
    legacy.set_state("run", {"status": "paused", "owner": "control", "generation": "old"})
    legacy.write("memories", {"schema_version": 1, "id": "memory", "kind": "experience", "content": "private-marker",
                 "source": {"type": "interaction", "recorded_at": "2026-10-23T00:00:00Z", "reference": "unknown"},
                 "confidence": "observed", "tags": [], "locked": False}, None, "test", "memory")
    snapshot = capture(legacy.root, legacy.profile)
    # A fresh target uses the already-provisioned test schema, after removing only
    # the fixture's initial defaults (no production database can reach this path).
    with pg_store.db() as db:
        db.execute("DELETE FROM record_owners")
        db.execute("DELETE FROM source_dependencies")
        db.execute("DELETE FROM governance_policies")
        db.execute("DELETE FROM content_records")
        db.execute("DELETE FROM state")
    target = PostgresStore(tmp_path / "new-root", pg_store.database._options, secrets.token_bytes(32))
    try:
        report = apply(target, snapshot)
        assert report["counts"]["memories"] == 1 and report["quarantined"] >= 7
        assert target.read("memories", "memory") == legacy.read("memories", "memory")
        assert target.state("core-model:2026-10-23") == 17
        assert target.state("run")["status"] == "paused"
        assert GovernanceRepository(target).policy("memories/memory").classification == "unclassified"
        assert apply(target, snapshot)["reused"]
        assert capture(legacy.root, legacy.profile)["fingerprint"] == snapshot["fingerprint"]
        with pytest.raises(AssistantError):
            apply(target, {**snapshot, "fingerprint": "different"})
    finally:
        target.database.close()
