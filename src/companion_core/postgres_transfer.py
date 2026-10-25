"""Bounded, authenticated application snapshots and explicit legacy import.

No SQL from an archive is executed. Table/column names come from the installed
schema; all values are parameters. Payload and access metadata are encrypted in
the exported artifact. The database key is deliberately not included.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import zlib
from datetime import date, datetime

from companion_core.runtime_keys import RUN_KEYS

from .cipher import ContentCipher
from .errors import AssistantError
from .governance import GovernanceRepository

MAX_BACKUP = 512 * 1024 * 1024
TABLES = (
    "content_records", "content_history", "content_audit", "sessions", "state", "actions", "events",
    "relationship_pending", "core_events", "core_actions", "core_identity_links", "core_observations",
    "governance_policies", "governance_grants", "governance_audit", "governance_sessions", "governance_challenges",
    "record_owners", "source_dependencies", "grant_resources", "core_event_receipts", "core_delivery_state",
    "actor_nodes", "relationship_claims", "asset_restore_jobs", "governance_provenance", "conversation_index", "input_image_blobs",
    "personal_fact_index",
)


def _identifier(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise AssistantError("snapshot_schema", "快照表结构无效")
    return '"' + value + '"'


def _encode(value):
    if isinstance(value, (bytes, memoryview)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (datetime, date)):
        return {"$time": value.isoformat()}
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return base64.b64decode(value["$bytes"], validate=True)
        if set(value) == {"$time"}:
            return datetime.fromisoformat(value["$time"])
        # JSONB is explicitly adapted, not interpolated as SQL.
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    return value


def tables(store):
    return (*TABLES, *sorted(store.profile.extra_tables))


def _snapshot(store, db):
    result = {}
    for table in tables(store):
        cursor = db.execute("SELECT * FROM " + _identifier(table))
        columns = [column.name for column in cursor.cursor.description]
        rows = [[_encode(value) for value in row] for row in cursor]
        rows.sort(key=lambda value: json.dumps(value, sort_keys=True))
        result[table] = {"columns": columns, "rows": rows}
    migrations = [list(row) for row in db.execute("SELECT version,checksum FROM schema_migrations ORDER BY version")]
    return {"format": 1, "migrations": migrations, "tables": result}


def _raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(store):
    with store.db() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return hashlib.sha256(_raw(_snapshot(store, db))).hexdigest()


def create(store, backup_key, *, metadata=None):
    from . import asset_transfer

    with store.lock, store.db() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = _snapshot(store, db)
        if snapshot["tables"]["asset_restore_jobs"]["rows"]:
            raise AssistantError("restore_pending", "请先完成上次图片恢复")
        snapshot["assets"] = asset_transfer.capture(store)
    raw = _raw(snapshot)
    if metadata is not None:
        metadata["raw_bytes"] = len(raw)
    if len(raw) > MAX_BACKUP:
        raise AssistantError("backup_limit", "备份超过当前应用快照上限，请使用受保护的 PostgreSQL 原生备份")
    identifier = secrets.token_hex(16)
    encrypted = ContentCipher(backup_key).encrypt(zlib.compress(raw, 6), "backup", identifier, "1")
    return b"CCBACKUP1:" + identifier.encode("ascii") + b"\n" + encrypted


def unpack(blob, backup_key):
    if len(blob) > MAX_BACKUP or not blob.startswith(b"CCBACKUP1:") or blob[42:43] != b"\n":
        raise AssistantError("backup_invalid", "备份格式无效或超过上限")
    try:
        identifier = blob[10:42].decode("ascii")
        compressed = ContentCipher(backup_key).decrypt(blob[43:], "backup", identifier, "1")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_BACKUP + 1)
        if len(raw) > MAX_BACKUP or not decoder.eof or decoder.unused_data:
            raise ValueError()
        result = json.loads(raw)
        if result.get("format") != 1:
            raise ValueError()
        return result
    except (ValueError, UnicodeError, zlib.error):
        raise AssistantError("backup_invalid", "备份内容无效或完整性校验失败") from None


def restore(store, blob, backup_key, *, expected):
    from . import asset_transfer

    snapshot = unpack(blob, backup_key)
    if set(snapshot.get("tables", {})) != set(tables(store)):
        raise AssistantError("backup_schema", "备份与当前资料类型不一致")
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _snapshot(store, db)
        if not expected or hashlib.sha256(_raw(current)).hexdigest() != expected:
            raise AssistantError("edit_conflict", "恢复预览后资料已经变化，请重新检查")
        if snapshot["migrations"] != current["migrations"]:
            raise AssistantError("backup_schema", "备份数据库版本不一致，请先执行明确的版本迁移")
        for key in RUN_KEYS:
            if (store.state(key, {}, db) or {}).get("status") == "active":
                raise AssistantError("restore_active", "恢复前需要先停止角色")
        for table in tables(store):
            if snapshot["tables"][table]["columns"] != current["tables"][table]["columns"]:
                raise AssistantError("backup_schema", "备份字段不匹配")
        for table in reversed(tables(store)):
            db.execute("DELETE FROM " + _identifier(table))
        for table in tables(store):
            block = snapshot["tables"][table]
            columns = block["columns"]
            query = "INSERT INTO " + _identifier(table) + "(" + ",".join(map(_identifier, columns)) + ")"
            if table == "content_audit":
                query += " OVERRIDING SYSTEM VALUE"
            query += " VALUES(" + ",".join("?" for _ in columns) + ")"
            db.executemany(query, [tuple(_decode(value) for value in row) for row in block["rows"]])
        db.execute("SELECT setval(pg_get_serial_sequence('content_audit','id'),COALESCE(MAX(id),1),COUNT(*)>0) FROM content_audit")
        # Decode and validate all records before committing; a mismatched database
        # encryption key must not leave a partially restored service.
        if not store.validate_all()["ok"]:
            raise AssistantError("backup_invalid", "恢复资料未通过结构和解密校验")
        from .input_images import verify_blobs

        verify_blobs(store)
        GovernanceRepository(store).reset_after_restore()
        from .reply_authorization import ReplyAuthorizations

        ReplyAuthorizations(store).reset_after_restore()
        for table in ("actions", "core_actions"):
            db.execute("UPDATE " + table + " SET status='cancelled' WHERE status='prepared'")
            db.execute("UPDATE " + table + " SET status='uncertain' WHERE status='dispatching'")
        asset_transfer.enqueue(store, snapshot.get("assets", []), db)
    asset_transfer.finish(store)
    return {"restored": True, "role_started": False, "sessions_revoked": True, "grants_require_review": True}
