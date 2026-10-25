"""Only generated encrypted SQLite fixtures; no real client/accounts/keys."""

import builtins
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from companion_wechat import db_sqlite

SYNTHETIC_KEY = bytes(range(32))


@pytest.fixture
def driver():
    if os.name != "nt":
        pytest.skip("Windows-only native VFS integration")
    return pytest.importorskip("sqlcipher3")


@pytest.fixture
def encrypted_database(tmp_path, driver):
    path = tmp_path / "synthetic # db.db"
    # A writable SQLCipher connection must never exist in the reader process:
    # Windows VFS otherwise reuses its process-local writable SHM mapping.
    script = r'''
import json,sys,sqlcipher3
c=sqlcipher3.connect(sys.argv[1],isolation_level=None,timeout=0.1)
c.set_key("x'"+bytes(range(32)).hex()+"'")
for sql in ["PRAGMA cipher_compatibility=4", "PRAGMA temp_store=MEMORY",
            "PRAGMA journal_mode=WAL", "PRAGMA wal_autocheckpoint=0",
            "CREATE TABLE fixture(id INTEGER PRIMARY KEY, content TEXT)",
            "INSERT INTO fixture VALUES(1, 'SYNTHETIC private-looking text')"]:
    c.execute(sql)
print("ready",flush=True)
for line in sys.stdin:
    query=json.loads(line)
    if query is None:
        break
    rows=c.execute(query).fetchall()
    print(json.dumps(rows),flush=True)
c.close()
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "ready"

    class RemoteWriter:
        def execute(self, sql):
            process.stdin.write(json.dumps(sql) + "\n")
            process.stdin.flush()
            self.rows = json.loads(process.stdout.readline())
            return self

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def close(self):
            if process.poll() is None:
                process.stdin.write("null\n")
                process.stdin.flush()
                process.wait(timeout=10)

    writer = RemoteWriter()
    try:
        yield path, writer
    finally:
        writer.close()
        process.communicate(timeout=10)


def _files(path):
    return {suffix: Path(str(path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}


def test_raw_key_literal_has_no_sql_statement_and_is_mutable():
    value = db_sqlite._key_literal(SYNTHETIC_KEY)
    assert isinstance(value, bytearray)
    assert value == ("x'" + SYNTHETIC_KEY.hex() + "'").encode()


@pytest.mark.parametrize("key", [b"short", b"x" * 48, "PRIVATE", None, [1] * 32])
def test_invalid_key_diagnostics_never_echo_input(key):
    with pytest.raises(db_sqlite.DatabaseReadError, match="^key_invalid$"):
        db_sqlite._key_literal(key)


@pytest.mark.parametrize("value", [0, -1, 31, float("nan"), float("inf"), True, "PRIVATE"])
def test_invalid_query_budget_fails_before_path_access(value):
    with pytest.raises(db_sqlite.DatabaseReadError, match="^configuration_invalid$"):
        with db_sqlite.open_readonly("PRIVATE", SYNTHETIC_KEY, query_timeout=value):
            pytest.fail("not reached")


@pytest.mark.parametrize("path", ["https://example.invalid/PRIVATE.db", r"\\server\PRIVATE.db", "file:PRIVATE.db"])
def test_non_local_or_uri_input_rejected(path):
    with pytest.raises(db_sqlite.DatabaseReadError, match="^path_invalid$"):
        db_sqlite._source_path(path)


def test_unknown_exception_code_cannot_export_private_data():
    error = db_sqlite.DatabaseReadError("PRIVATE")
    assert error.code == str(error) == "query_failed"


def test_lazy_dependency_import(monkeypatch):
    original = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "sqlcipher3":
            pytest.fail("must not load native SQLCipher at module import")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    importlib.reload(db_sqlite)


def test_missing_driver_is_sanitized(monkeypatch):
    def missing(name):
        raise db_sqlite.metadata.PackageNotFoundError("PRIVATE")

    monkeypatch.setattr(db_sqlite.metadata, "version", missing)
    with pytest.raises(db_sqlite.DatabaseReadError, match="^dependency_missing$"):
        db_sqlite._load_sqlcipher()


def test_unverified_driver_version_fails_closed(monkeypatch):
    monkeypatch.setattr(db_sqlite.metadata, "version", lambda _: "99.0")
    with pytest.raises(db_sqlite.DatabaseReadError, match="^dependency_version_unsupported$"):
        db_sqlite._load_sqlcipher()


def test_native_query_has_named_rows_and_bounded_memory_settings(encrypted_database):
    path, _ = encrypted_database
    with db_sqlite.open_readonly(path, SYNTHETIC_KEY) as connection:
        row = connection.execute("SELECT id, content FROM fixture").fetchone()
        assert row["id"] == row[0] == 1
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert int(connection.execute("PRAGMA cipher_memory_security").fetchone()[0]) == 1
        assert connection.execute("PRAGMA cache_size").fetchone()[0] == -2048
    assert path.read_bytes()[:16] != b"SQLite format 3\0"
    with pytest.raises(db_sqlite.DatabaseReadError, match="^connection_closed$"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("sql", [
    "INSERT INTO fixture VALUES(2, 'PRIVATE')", "DELETE FROM fixture", "DROP TABLE fixture",
    "CREATE TEMP TABLE PRIVATE(value)", "PRAGMA query_only=OFF", "PRAGMA journal_mode=DELETE",
    "PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA cipher_log='PRIVATE.log'",
    "ATTACH DATABASE 'PRIVATE.db' AS other", "SELECT load_extension('PRIVATE.dll')",
    "SELECT sqlcipher_export('PRIVATE')", "SELECT randomblob(1000000000)",
])
def test_write_escape_and_extension_operations_are_denied(encrypted_database, sql):
    path, _ = encrypted_database
    before = _files(path)
    with db_sqlite.open_readonly(path, SYNTHETIC_KEY) as connection:
        with pytest.raises(db_sqlite.DatabaseReadError, match="^query_denied$"):
            connection.execute(sql).fetchall()
    assert _files(path) == before


def test_query_failure_never_exposes_sql_or_values(encrypted_database):
    path, _ = encrypted_database
    with db_sqlite.open_readonly(path, SYNTHETIC_KEY) as connection:
        with pytest.raises(db_sqlite.DatabaseReadError) as caught:
            connection.execute("SELECT PRIVATE FROM no_such_PRIVATE_table")
        assert str(caught.value) == "query_failed"


def test_progress_handler_limits_expensive_queries(encrypted_database):
    path, _ = encrypted_database
    with db_sqlite.open_readonly(path, SYNTHETIC_KEY, query_timeout=0.02) as connection:
        with pytest.raises(db_sqlite.DatabaseReadError, match="^query_timeout$"):
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
                "SELECT sum(x) FROM n"
            ).fetchone()
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_pin_prevents_sidecar_deletion_until_connection_closes(encrypted_database):
    path, _ = encrypted_database
    with db_sqlite.open_readonly(path, SYNTHETIC_KEY):
        for suffix in ("", "-wal", "-shm"):
            with pytest.raises(OSError):
                Path(str(path) + suffix).unlink()


def test_missing_sidecars_fail_without_creating_any_files(encrypted_database):
    path, writer = encrypted_database
    writer.close()
    before = {p.name for p in path.parent.iterdir()}
    with pytest.raises(db_sqlite.DatabaseReadError, match="^sidecars_unavailable$"):
        with db_sqlite.open_readonly(path, SYNTHETIC_KEY):
            pytest.fail("not reached")
    assert {p.name for p in path.parent.iterdir()} == before


def test_wrong_key_fails_without_modifying_encrypted_sources(encrypted_database, capfd):
    path, _ = encrypted_database
    before = _files(path)
    with pytest.raises(db_sqlite.DatabaseReadError) as caught:
        with db_sqlite.open_readonly(path, b"z" * 32):
            pytest.fail("not reached")
    assert caught.value.code in {"database_unreadable", "query_failed"}
    assert _files(path) == before
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""


def test_uncommitted_writer_rows_are_invisible(encrypted_database):
    path, writer = encrypted_database
    writer.execute("BEGIN")
    writer.execute("INSERT INTO fixture VALUES(2, 'uncommitted synthetic row')")
    try:
        with db_sqlite.open_readonly(path, SYNTHETIC_KEY) as connection:
            assert connection.execute("SELECT count(*) FROM fixture").fetchone()[0] == 1
    finally:
        writer.execute("ROLLBACK")


def test_independent_reader_uses_readonly_shm_and_real_wal_snapshot(encrypted_database):
    path, writer = encrypted_database
    # The writer and reader must be different processes: sharing SQLite's
    # process-local VFS node could hide a mistakenly writable SHM connection.
    module = Path(db_sqlite.__file__).resolve()
    script = r'''
import json,sys
from companion_wechat import db_sqlite as module
with module.open_readonly(sys.argv[2],bytes(range(32))) as c:
    c.execute("BEGIN")
    print(c.execute("SELECT count(*) FROM fixture").fetchone()[0],flush=True)
    sys.stdin.readline()
    old=c.execute("SELECT count(*) FROM fixture").fetchone()[0]
    c.execute("ROLLBACK")
    new=c.execute("SELECT count(*) FROM fixture").fetchone()[0]
    print(json.dumps([old,new]),flush=True)
'''
    before = _files(path)
    process = subprocess.Popen([sys.executable, "-c", script, str(module), str(path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "1"
        assert _files(path) == before
        writer.execute("BEGIN")
        writer.execute("INSERT INTO fixture VALUES(2, 'uncommitted synthetic row')")
        writer.execute("COMMIT")
        # The reader's old snapshot pins the last valid WAL commit.
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 1
        process.stdin.write("continue\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline()) == [1, 2]
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
def test_binary_text_column_is_preserved(encrypted_database):
    path, _ = encrypted_database
    with db_sqlite.open_readonly(path, SYNTHETIC_KEY) as conn:
        assert conn.execute("SELECT CAST(X'80' AS TEXT)").fetchone()[0] == b"\x80"

