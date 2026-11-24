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
from pathlib import Path

from companion_core.runtime_keys import RUN_KEYS

from .cipher import ContentCipher
from .errors import AssistantError
from .governance import GovernanceRepository

MAX_BACKUP = 512 * 1024 * 1024
# Renewable application credentials belong to this installation, never a role snapshot.
LOCAL_STATE_KEYS = frozenset({'desktop-siwc-validation', 'desktop-qq-account'})
TABLES = (
    "content_records", "content_history", "content_audit", "sessions", "state", "actions", "events",
    "relationship_pending", "core_events", "core_actions", "core_identity_links", "core_observations",
    "governance_policies", "governance_grants", "governance_audit", "governance_sessions", "governance_challenges",
    "record_owners", "source_dependencies", "grant_resources", "source_retention", "core_event_receipts", "core_delivery_state",
    "actor_nodes", "relationship_claims", "asset_restore_jobs", "governance_provenance", "conversation_index", "input_image_blobs",
    "personal_fact_index", "qq_native_inbox", "media_originals", "media_original_holds", "sticker_library", "file_library",
    "library_version_bodies", "library_versions", "preset_reply_turns", "role_reminders",
    "privacy_jobs", "privacy_slices", "matter_contacts", "role_matters", "matter_watches", "privacy_dependencies", "privacy_review_sources",
    "privacy_corrections","role_activity",
)
VERSION_TABLES = {
    "library_version_bodies": ["kind", "id", "sha256", "size", "body"],
    "library_versions": ["kind", "id", "position", "revision", "sha256", "size", "metadata", "archived_at"],
}
ADDITIVE_TABLES = {17: VERSION_TABLES, 18: {"preset_reply_turns": ["kind", "id", "preset_id", "sent_at"]},
                   19: {"role_reminders": ["id", "scope", "platform", "account", "due", "status", "version",
                                            "body", "attempt", "wake_after"]},
                   20: {"privacy_jobs": ["resource","source_version","cursor_pos","due","complete"],
                        "privacy_slices": ["id","resource","source_version","version","status","level","category","fingerprint","body"],
                        "matter_contacts": ["id","platform","account","thread","actor","anchor","occurred","permission"],
                        "role_matters": ["id","version","status","delivery","platform","account","wake","attempt","body"],
                        "matter_watches": ["matter","contact"]},
                   21: {"privacy_dependencies": ["derived","fragment","revision"]},
                   22: {"privacy_review_sources": ["resource","context_source"]},
                   23: {"privacy_corrections":["resource","fingerprint","level"]},
                   24: {"role_activity":["matter","updated_at"]},
                   25: {}}  # Trigger-only migration; the serialized table layout is unchanged.


def upgrade_version_tables(snapshot):
    """Older snapshots can supply empty tables for features added since export."""
    versions = snapshot.get("migrations")
    blocks = snapshot.get("tables")
    if (not isinstance(versions, list) or not isinstance(blocks, dict) or len(versions) not in range(16, max(ADDITIVE_TABLES)) or
            [v[0] for v in versions if isinstance(v, list) and len(v) == 2] != list(range(1, len(versions) + 1))):
        return snapshot
    missing = {t for n, fields in ADDITIVE_TABLES.items() if n > len(versions) for t in fields}
    if missing & blocks.keys() or not (set(TABLES) - missing) <= blocks.keys():
        return snapshot
    for n, fields in ADDITIVE_TABLES.items():
        if n > len(versions):
            migration, = Path(__file__).with_name("sql").glob(f"{n:03d}_*.sql")
            checksum = hashlib.sha256(migration.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            versions.append([n, checksum])
            blocks.update({table: {"columns": columns.copy(), "rows": []} for table, columns in fields.items()})
    return snapshot


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
        if table == 'state':
            index = columns.index('key')
            rows = [row for row in rows if row[index] not in LOCAL_STATE_KEYS]
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
    from . import backup_stream
    if blob.startswith(backup_stream.MAGIC):
        import io
        if len(blob) > MAX_BACKUP:
            raise AssistantError('backup_limit','备份超过此导入入口的容量上限')
        result = {"format": 2, "tables": {}, "assets": []}
        for item in backup_stream.read(io.BytesIO(blob), backup_key, raw_limit=MAX_BACKUP):
            if item[0] == "schema":
                result["migrations"] = item[1]
            elif item[0] == "table":
                block = result["tables"][item[1]] = {"columns":item[2], "rows":[]}
            elif item[0] == "row":
                block["rows"].append(item[1])
            elif item[0] == "asset":
                result["assets"].append(item[1])
        return upgrade_version_tables(result)
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
        return upgrade_version_tables(result)
    except (ValueError, UnicodeError, zlib.error):
        raise AssistantError("backup_invalid", "备份内容无效或完整性校验失败") from None


def restore(store, blob, backup_key, *, expected, finalize=None):
    from . import asset_transfer

    snapshot = unpack(blob, backup_key)
    saved_tables = set(snapshot.get("tables", {}))
    # Pre-Bilibili desktop backups contain all common tables but no video usage.
    extend_videos = store.profile.extra_tables == {"videos"} and saved_tables == set(TABLES) and bool(store.state("desktop-owner"))
    if not extend_videos and saved_tables != set(tables(store)):
        raise AssistantError("backup_schema", "备份与当前资料类型不一致")
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _snapshot(store, db)
        if not expected or hashlib.sha256(_raw(current)).hexdigest() != expected:
            raise AssistantError("edit_conflict", "恢复预览后资料已经变化，请重新检查")
        if snapshot["migrations"] != current["migrations"]:
            raise AssistantError("backup_schema", "备份数据库版本不一致，请先执行明确的版本迁移")
        if extend_videos:
            snapshot["tables"]["videos"] = {"columns":current["tables"]["videos"]["columns"],"rows":[]}
        for key in RUN_KEYS:
            if (store.state(key, {}, db) or {}).get("status") == "active":
                raise AssistantError("restore_active", "恢复前需要先停止角色")
        for table in tables(store):
            if snapshot["tables"][table]["columns"] != current["tables"][table]["columns"]:
                raise AssistantError("backup_schema", "备份字段不匹配")
        local_state = {key: store.state(key, db=db) for key in LOCAL_STATE_KEYS}
        for table in reversed(tables(store)):
            db.execute("DELETE FROM " + _identifier(table))
        for table in tables(store):
            block = snapshot["tables"][table]
            columns = block["columns"]
            query = "INSERT INTO " + _identifier(table) + "(" + ",".join(map(_identifier, columns)) + ")"
            if table == "content_audit":
                query += " OVERRIDING SYSTEM VALUE"
            query += " VALUES(" + ",".join("?" for _ in columns) + ")"
            rows = block['rows']
            if table == 'state':
                index = columns.index('key')
                rows = [row for row in rows if row[index] not in LOCAL_STATE_KEYS]
            db.executemany(query, [tuple(_decode(value) for value in row) for row in rows])
        for key, value in local_state.items():
            if value is not None:
                store.set_state(key, value, db)
        finish_restore(store, db, snapshot.get('assets', []), finalize)
    asset_transfer.finish(store)
    return {"restored": True, "role_started": False, "sessions_revoked": True, "grants_require_review": True}


def finish_restore(store, db, assets, finalize):
    from . import asset_transfer
    from .context_workspace import import_previous_settings

    import_previous_settings(store, db)
    # Decode and validate all records before committing; a mismatched database
    # encryption key must not leave a partially restored service.
    if not store.validate_critical()["ok"]:
        raise AssistantError("backup_invalid", "恢复资料未通过结构和解密校验")
    with db.stream("SELECT kind,id,version,body FROM content_records") as rows:
        for row in rows:
            store._decode(row["kind"], row["id"], row["version"], row["body"])
    from .input_images import verify_blobs

    verify_blobs(store)
    from .media_originals import verify_originals

    verify_originals(store)
    from .sticker_library import verify_stickers

    verify_stickers(store)
    from .file_library import verify_files
    verify_files(store)
    from .library_versions import verify_versions

    verify_versions(store)
    from .portable_validation import ROLE_BODY_PAYLOADS

    for field in ROLE_BODY_PAYLOADS:
        with db.stream("SELECT id,body FROM " + _identifier(field.table)) as rows:
            for row in rows:
                if not isinstance(store.ledger_decode(field.prefix + row["id"], row["body"]), dict):
                    raise AssistantError("backup_invalid", "恢复资料的加密正文结构无效")
    # Restore requires stopped tasks. Holds belonged to exited task owners;
    # their processing receipts and unfinished events remain in the snapshot.
    db.execute('DELETE FROM media_original_holds')
    GovernanceRepository(store).reset_after_restore()
    from .reply_authorization import ReplyAuthorizations

    ReplyAuthorizations(store).reset_after_restore()
    db.execute("UPDATE role_matters SET status=CASE WHEN attempt IS NULL THEN 'stopped' ELSE 'uncertain' END,"
               "delivery=CASE WHEN attempt IS NULL THEN delivery ELSE 'uncertain' END,"
               "version=md5(version || ':restore') WHERE status IN ('ready','waiting','processing')")
    db.execute("DELETE FROM matter_contacts")
    db.execute("DELETE FROM matter_watches")
    # A restored snapshot cannot prove no send occurred after it was taken.
    db.execute("UPDATE role_reminders SET status=CASE WHEN status='processing' THEN 'uncertain' ELSE 'cancelled' END,"
               "version=md5(version || ':restore'),attempt=NULL WHERE status IN ('pending','processing')")
    db.execute("DELETE FROM state WHERE key LIKE 'alarm-source:%' OR key LIKE 'group-mode:%' OR key LIKE 'group-samples:%'")
    for table in ("actions", "core_actions"):
        db.execute("UPDATE " + table + " SET status='cancelled' WHERE status='prepared'")
        db.execute("UPDATE " + table + " SET status='uncertain' WHERE status='dispatching'")
    asset_transfer.enqueue(store, assets, db)
    # The desktop runtime can advance this sequence but does not own the table.
    # Advance only after consuming the archive. A later rollback may leave a gap
    # but will never reuse an audit ID from the original database.
    db.execute("""SELECT setval('content_audit_id_seq', restored.highest, true)
                  FROM content_audit_id_seq, (SELECT MAX(id) AS highest FROM content_audit) AS restored
                  WHERE last_value < restored.highest OR (last_value = restored.highest AND NOT is_called)""")
    if finalize is not None:
        finalize(db)
