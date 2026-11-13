import pytest

from companion_core.errors import AssistantError
from companion_core.postgres import verify_schema


def test_runtime_schema_validation_is_read_only_and_detects_missing_version(pg_store):
    verify_schema(pg_store.database)
    with pg_store.db() as db:
        db.execute('DELETE FROM schema_migrations WHERE version=12')
    with pytest.raises(AssistantError) as error:
        verify_schema(pg_store.database)
    assert error.value.code == 'storage_schema_upgrade_required'
    with pg_store.db() as db:
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=12').fetchone()[0] == 0


def test_newer_and_changed_schema_refused(pg_store):
    with pg_store.db() as db:
        db.execute("INSERT INTO schema_migrations VALUES(999,'unknown')")
    with pytest.raises(AssistantError) as error:
        pg_store.migrate()
    assert error.value.code == 'migration_version'
    with pg_store.db() as db:
        db.execute('DELETE FROM schema_migrations WHERE version=999')
        db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=12")
    with pytest.raises(AssistantError) as error:
        verify_schema(pg_store.database)
    assert error.value.code == 'migration_checksum'
