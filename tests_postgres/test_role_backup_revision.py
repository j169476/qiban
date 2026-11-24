"""Effective SQL writes and stale automatic archives across the v24 upgrade."""

import secrets
import shutil
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from companion_core import backup_stream, events, postgres, postgres_transfer, reminders
from companion_core.backup_maintenance import BackupMaintenance
from companion_core.postgres import PostgresStore
from companion_core.reply_authorization import ReplyAuthorizations
from tests_postgres.matter_fixture import legacy_alarm
from tests_postgres.test_windows_fragment_review import source


def revision(db):
    return db.execute("SELECT revision FROM maintenance_revision WHERE singleton").fetchone()[0]


# Use raw SQL including composite keys, nullable columns and both foreign keys.
# The first column identifies the synthetic row through each check.
ROWS = {
    "role_reminders": (dict(id="fixture", scope="s", platform="qq", account="a", due=1, status="pending",
                            version="v", body="body"), "due", 2),
    "role_matters": (dict(id="fixture", version="v", status="waiting", delivery="sent", platform="qq",
                          account="a", wake=1, body="body"), "wake", 2),
    "matter_contacts": (dict(id="fixture", platform="qq", account="a", thread="t", actor="p", anchor="e",
                             occurred="2026-01-01", permission="v"), "permission", "v2"),
    "matter_watches": (dict(matter="fixture", contact="a"), "contact", "b"),
    "role_activity": (dict(matter="fixture", updated_at=1), "updated_at", 2),
    "privacy_jobs": (dict(resource="fixture", source_version="v"), "complete", 1),
    "privacy_slices": (dict(id="fixture", resource="r", source_version="v", version="v", status="reviewed",
                            category="ordinary", body="body"), "level", "private"),
    "privacy_dependencies": (dict(derived="fixture", fragment="f", revision="v"), "revision", "v2"),
    "privacy_review_sources": (dict(resource="fixture", context_source="a"), "context_source", "b"),
    "privacy_corrections": (dict(resource="fixture", fingerprint="f", level="social"), "level", "private"),
}


def insert_sql(table, row):
    return f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})"


@pytest.mark.parametrize("table", ROWS)
def test_effective_direct_sql_changes_revision(pg_store, table):
    row, field, change = ROWS[table]
    values = tuple(row.values())
    insert = insert_sql(table, row)
    identity = next(iter(row))
    where = f" WHERE {identity}=?"
    update = f"UPDATE {table} SET {field}=?{where}"
    delete = f"DELETE FROM {table}{where}"
    with pg_store.db() as db:
        if table in ("matter_watches", "role_activity"):
            parent = ROWS["role_matters"][0]
            db.execute(insert_sql("role_matters", parent), tuple(parent.values()))
        start = revision(db)
        db.execute(update, (change, "absent"))
        db.execute(delete, ("absent",))
        assert revision(db) == start
        db.execute(insert, values)
        assert revision(db) == start + 1
        db.execute(insert + " ON CONFLICT DO NOTHING", values)
        db.execute(f"UPDATE {table} SET {field}={field}{where}", ("fixture",))
        assert revision(db) == start + 1
        db.execute("SAVEPOINT before_change")
        db.execute(update, (change, "fixture"))
        assert revision(db) == start + 2
        db.execute("ROLLBACK TO SAVEPOINT before_change")
        assert revision(db) == start + 1
        db.execute(update, (change, "fixture"))
        assert revision(db) == start + 2
        db.execute(delete, ("fixture",))
        assert revision(db) == start + 3
    with pg_store.db() as db:
        assert revision(db) == start + 3


def test_batch_upserts_ignore_empty_and_identical_transitions(pg_store):
    upsert = """INSERT INTO privacy_jobs(resource,source_version) VALUES ('A','v'),('B','v')
                ON CONFLICT(resource) DO UPDATE SET source_version=excluded.source_version"""
    with pg_store.db() as db:
        start = revision(db)
        db.execute(upsert)
        assert revision(db) == start + 1
        db.execute(upsert)
        db.execute("UPDATE privacy_jobs SET due=1 WHERE complete=0 AND resource='missing'")
        db.execute("INSERT INTO privacy_jobs SELECT * FROM privacy_jobs WHERE false")
        assert revision(db) == start + 1
        db.execute("UPDATE privacy_jobs SET due=CASE WHEN resource='A' THEN 1 ELSE due END "
                   "WHERE resource IN ('A','B')")
        assert revision(db) == start + 2
        db.execute("DELETE FROM privacy_jobs WHERE resource IN ('A','B')")
        assert revision(db) == start + 3
    # A backup on a separate connection can only observe committed revisions.
    with psycopg.connect(**pg_store.database._options) as writer:
        writer.execute("INSERT INTO privacy_jobs(resource,source_version) VALUES ('uncommitted','v')")
        with pg_store.db() as db:
            assert revision(db) == start + 3
        writer.rollback()
    with pg_store.db() as db:
        assert revision(db) == start + 3
        assert db.execute("SELECT 1 FROM privacy_jobs WHERE resource='uncommitted'").fetchone() is None


@pytest.fixture
def v24_space(pg_store, tmp_path, monkeypatch):
    # Create a genuinely empty namespace and install only the SQL shipped in v24.
    schema = "backup_upgrade_" + secrets.token_hex(12)
    options = pg_store.database._options
    scoped = {**options, "options": "-c search_path=" + schema}
    key = secrets.token_bytes(32)
    old_resources = tmp_path / "v24" / "sql"
    old_resources.mkdir(parents=True)
    for script in Path(postgres.__file__).with_name("sql").glob("[0-9]*.sql"):
        if int(script.name.split("_")[0]) <= 24:
            shutil.copyfile(script, old_resources / script.name)
    with psycopg.connect(**options, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with ExitStack() as pools:
            def open_store():
                store = PostgresStore(tmp_path / "upgrade-data", scoped, key)
                pools.callback(store.database.close)
                return store

            store = open_store()
            with monkeypatch.context() as patch:
                patch.setattr(postgres, "__file__", str(old_resources.parent / "postgres.py"))
                store.migrate()
                assert store.initialize()["ok"]
            yield store, open_store
    finally:
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_v24_stale_backup_refreshes_once_and_restores(v24_space, tmp_path, record_property):
    store, reopen = v24_space
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dates = [start + timedelta(days=days) for days in (2, 4, 5, 6)]
    now = [start.timestamp()]
    event = source("synthetic reminder", "10002", "10002", 1, start, "private")
    scope = reminders.scope_for(store, event)
    ReplyAuthorizations(store).update(scope, enabled=True, expected_version=None, actor="fixture")
    events.ingest(store, event)
    alarm = legacy_alarm(store, event, dates[0], "synthetic reminder")
    with store.db() as db:
        assert backup_stream.migrations(db)[-1][0] == 24
        for status in ("succeeded", "dispatching", "prepared"):
            db.execute("""INSERT INTO core_actions(id,dedupe,generation,platform,account,capability,body,status,created)
                       VALUES(?,?,'gen','qq','a','message.reply',?,?,?)""",
                       (status, status, store.ledger_encode("action:" + status, {}), status, start.isoformat()))
    key = store._backup_key
    portable = tmp_path / "v24-legacy.ccbackup"
    portable.write_bytes(postgres_transfer.create(store, key))
    worker = BackupMaintenance(store, clock=lambda: now[0])
    folder = store.root / "history" / "backups"

    def cycle(previous):
        now[0] = previous["next_at"]
        saved = worker.step()
        assert saved["status"] == "saved"
        return saved

    def move(alarm, date):
        return reminders.change(store, alarm["id"], alarm["version"], "reschedule", now[0], due_at=date.isoformat())

    first = worker.step()
    assert first["status"] == "saved"
    alarm = move(alarm, dates[1])
    stale = cycle(first)
    assert (stale["revision"], stale["filename"]) == (first["revision"], first["filename"])
    store.database.close()
    store = reopen()
    store.migrate()
    with store.db() as db:
        assert revision(db) == first["revision"] + 1
        assert backup_stream.migrations(db)[-1][0] == 25
    store.database.close()
    store = reopen()
    store.migrate()
    postgres.verify_schema(store.database)
    worker = BackupMaintenance(store, clock=lambda: now[0])
    assert worker.step() is None
    refreshed = cycle(stale)
    assert refreshed["revision"] == first["revision"] + 1 and refreshed["filename"] != first["filename"]
    quiet = cycle(refreshed)
    assert quiet["filename"] == refreshed["filename"]
    alarm = move(alarm, dates[2])
    move(alarm, dates[3])
    assert worker.step() is None
    latest = cycle(quiet)
    assert latest["revision"] == refreshed["revision"] + 2
    assert cycle(latest)["filename"] == latest["filename"]
    archives = [(folder / state["filename"], date, version)
                for state, date, version in ((first, dates[0], 24), (refreshed, dates[1], 25), (latest, dates[3], 25))]
    assert set(folder.glob("*.ccbackup")) == {path for path, _, _ in archives}
    record_property("revisions", ",".join(str(state["revision"]) for state in (first, refreshed, latest)))
    for path, date, version in [(portable, dates[0], 24), *archives]:
        if path == portable:
            result = postgres_transfer.restore(store, path.read_bytes(), key, expected=postgres_transfer.fingerprint(store))
        else:
            with path.open("rb") as handle:
                assert next(backup_stream.read(handle, key))[1][-1][0] == version
            result = backup_stream.restore(store, path, key, expected=backup_stream.fingerprint(store),
                                           digest=backup_stream.inspect(path, key)["sha256"])
        assert result["restored"] and not result["role_started"]
        assert ReplyAuthorizations(store).version(scope) is None
        with store.db() as db:
            assert tuple(db.execute("SELECT due,status FROM role_reminders WHERE id=?", (alarm["id"],)).fetchone()) == (
                date.timestamp(), "cancelled")
            assert dict(db.execute("SELECT id,status FROM core_actions")) == {
                "succeeded": "succeeded", "dispatching": "uncertain", "prepared": "cancelled"}
        record_property(path.name, f"schema={version}, restored_due={date.date()}")
