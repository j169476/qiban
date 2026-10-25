import hashlib
import json
import secrets
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from companion_core import conversation_index, conversations, postgres
from companion_core.backup_maintenance import BackupMaintenance
from companion_core.errors import AssistantError
from companion_core.pg_roles import provision
from companion_core.postgres import PostgresStore


def test_previous_schema_upgrade_backfills_without_changing_content_or_role(pg_store, tmp_path, monkeypatch):
    options = pg_store.database._options
    schema = 'upgrade_test_' + secrets.token_hex(8)
    role, password = 'upgrade_app_' + secrets.token_hex(8), secrets.token_urlsafe(36)
    with psycopg.connect(**options, autocommit=True) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    scoped = {**options, 'options': '-c search_path=' + schema}
    key = secrets.token_bytes(32)
    owner = PostgresStore(tmp_path/'owner', scoped, key)
    app = None
    role_created = False
    try:
        with owner.db() as db:
            for path in sorted((Path(postgres.__file__).parent/'sql').glob('[0-9]*.sql'))[:5]:
                raw = path.read_bytes()
                db.executescript(raw.decode())
                db.execute('INSERT INTO schema_migrations VALUES(?,?)', (int(path.name.split('_')[0]), hashlib.sha256(raw).hexdigest()))
            for kind, spec in owner.profile.schemas.items():
                db.execute('INSERT INTO record_kinds VALUES(?,?::jsonb,?)', (kind, json.dumps(spec), kind in owner.profile.collections))
        owner.initialize()
        with monkeypatch.context() as patch:
            patch.setattr(conversation_index, 'write', lambda *args: None)
            conversations.append(owner, 'account', 'local', 'synthetic-thread', 'event', 'synthetic old history',
                'user', 'synthetic-actor', 'received', '2026-10-24T00:00:00Z', '2026-10-24T00:00:00Z')
        with owner.db() as db:
            before = [list(row) for row in db.execute('SELECT kind,id,version FROM content_records ORDER BY kind,id')]
        with psycopg.connect(**scoped) as db:
            provision(db, schema=schema, role=role, password=password)
        role_created = True
        app = PostgresStore(tmp_path/'app', {**scoped, 'user':role, 'password':password}, key)
        assert app.validate_critical()['ok']
        owner.migrate()
        owner.migrate()
        with psycopg.connect(**scoped) as db:
            for table in ('maintenance_revision', 'conversation_index', 'input_image_blobs', 'personal_fact_index'):
                db.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON {}.{} TO {}').format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(role)))
        with app.db() as db:
            after = [list(row) for row in db.execute('SELECT kind,id,version FROM content_records ORDER BY kind,id')]
            assert before == after
            assert db.execute('SELECT count(*) FROM conversation_index').fetchone()[0] == 1
            assert not db.execute("SELECT has_schema_privilege(current_user,?,'CREATE')", (schema,)).fetchone()[0]
            assert not db.execute("SELECT has_table_privilege(current_user,'schema_migrations','UPDATE')").fetchone()[0]
        assert BackupMaintenance(app).step()['status'] == 'saved'
        assert app.state('core-run') is None
    finally:
        if app:
            app.database.close()
        owner.database.close()
        with psycopg.connect(**options, autocommit=True) as db:
            if role_created:
                db.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_migration_line_endings_are_portable_but_changed_sql_is_rejected(pg_store, monkeypatch):
    path = sorted((Path(postgres.__file__).parent/'sql').glob('[0-9]*.sql'))[0]
    original = path.read_bytes().replace(b'\r\n', b'\n')
    historical = hashlib.sha256(original.replace(b'\n', b'\r\n')).hexdigest()
    with pg_store.db() as db:
        db.execute('UPDATE schema_migrations SET checksum=? WHERE version=1', (historical,))
    pg_store.migrate()
    with pg_store.db() as db:
        assert db.execute('SELECT checksum FROM schema_migrations WHERE version=1').fetchone()[0] == historical
    read = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda value: original + b'\n-- changed SQL\n' if value == path else read(value))
    with pytest.raises(AssistantError, match='升级文件发生变化'):
        pg_store.migrate()
