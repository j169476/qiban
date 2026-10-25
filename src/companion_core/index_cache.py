"""Disposable, hash-validated parsed records shared across CLI processes."""

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from functools import wraps

from . import __version__


def schema_version(kind, schema=None):
    if schema is None:
        from .profiles import default_profile
        schema = default_profile().schemas[kind]
    return hashlib.sha256((__version__ + json.dumps(schema, sort_keys=True)).encode()).hexdigest()


@contextmanager
def session(store):
    if getattr(store, "backend", "yaml") == "postgres":
        # Never project decrypted professional-store records into a plaintext
        # SQLite side cache. Its authorized index has a separate repository path.
        yield None
        return
    local = store._index_local
    if getattr(local, "active", False):
        yield local.db
        return
    db = None
    try:
        path = store.root / "cache" / "parsed.sqlite3"
        if path.is_symlink() or not path.resolve().is_relative_to(store.root):
            raise OSError("invalid cache path")
        path.parent.mkdir(exist_ok=True)
        db = sqlite3.connect(path, timeout=2)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("""CREATE TABLE IF NOT EXISTS parsed(
            kind TEXT, id TEXT, version TEXT, schema_version TEXT, data TEXT, data_hash TEXT,
            PRIMARY KEY(kind,id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS passages(
            kind TEXT, id TEXT, version TEXT, revision TEXT, spans TEXT, spans_hash TEXT,
            PRIMARY KEY(kind,id))""")
    except (OSError, sqlite3.Error):
        if db is not None:
            db.close()
        db = None
        store.index_stats["cache_errors"] += 1
    local.active, local.db = True, db
    try:
        yield db
    finally:
        local.active, local.db = False, None
        if db is not None:
            try:
                db.commit()
            except sqlite3.Error:
                store.index_stats["cache_errors"] += 1
            finally:
                db.close()


def indexed(method):
    @wraps(method)
    def wrapped(store, *args, **kwargs):
        with session(store):
            return method(store, *args, **kwargs)
    return wrapped


def load(store, kind, item_id, version):
    with session(store) as db:
        if db is None:
            return None
        try:
            row = db.execute("SELECT data,data_hash FROM parsed WHERE kind=? AND id=? AND version=? AND schema_version=?",
                             (kind, item_id, version, schema_version(kind, store.profile.schemas[kind]))).fetchone()
            if row and hashlib.sha256(row[0].encode()).hexdigest() == row[1]:
                data = json.loads(row[0])
                if isinstance(data, dict) and data.get("id") == item_id:
                    store.index_stats["persistent_hits"] += 1
                    return data
        except (sqlite3.Error, ValueError, TypeError):
            store.index_stats["cache_errors"] += 1
    return None


def save(store, kind, item_id, version, data):
    with session(store) as db:
        if db is not None:
            try:
                raw = json.dumps(data, ensure_ascii=False)
                db.execute("INSERT OR REPLACE INTO parsed VALUES (?,?,?,?,?,?)",
                           (kind, item_id, version, schema_version(kind, store.profile.schemas[kind]), raw, hashlib.sha256(raw.encode()).hexdigest()))
            except sqlite3.Error:
                store.index_stats["cache_errors"] += 1


def prune(store, kind, valid_ids):
    with session(store) as db:
        if db is not None:
            try:
                stale = [(kind, row[0]) for row in db.execute(
                    "SELECT id FROM parsed WHERE kind=? UNION SELECT id FROM passages WHERE kind=?", (kind, kind))
                    if row[0] not in valid_ids]
                db.executemany("DELETE FROM parsed WHERE kind=? AND id=?", stale)
                db.executemany("DELETE FROM passages WHERE kind=? AND id=?", stale)
            except sqlite3.Error:
                store.index_stats["cache_errors"] += 1


def reset(store):
    # Quarantine rather than trust or repair an arbitrary damaged disposable index.
    folder = (store.root / "cache").resolve()
    if not folder.is_relative_to(store.root):
        raise OSError("invalid cache path")
    suffix = ".invalid-" + uuid.uuid4().hex
    for name in ("parsed.sqlite3", "parsed.sqlite3-wal", "parsed.sqlite3-shm"):
        path = folder / name
        if path.exists():
            os.replace(path, path.with_name(name + suffix))
    store._read_cache.clear()
