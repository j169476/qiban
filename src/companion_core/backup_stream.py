"""Authenticated, ordered frames for local PostgreSQL backup files."""

import hashlib
import json
import os
import secrets
import struct
import tempfile
import zlib
from pathlib import Path

from . import asset_transfer
from .cipher import ContentCipher
from .errors import AssistantError
from .postgres_transfer import LOCAL_STATE_KEYS, _decode, _encode, _identifier, _raw, tables
from .runtime_keys import RUN_KEYS

MAGIC = b"CCBACKUP2\n"
CHUNK = 256 * 1024
MAX_LINE = 32 * 1024 * 1024


def invalid():
    return AssistantError("backup_invalid", "备份内容不完整或格式无效")


def migrations(db):
    return [
        list(row) for row in db.execute("SELECT version,checksum FROM schema_migrations ORDER BY version")
    ]


def records(store, db, *, ordered=False):
    yield ["schema", migrations(db)]
    for table in tables(store):
        name = _identifier(table)
        predicate = " WHERE key<>ALL(?)" if table == "state" else ""
        args = (list(LOCAL_STATE_KEYS),) if predicate else ()
        order = ""
        if ordered:
            keys = db.execute(
                "SELECT a.attname FROM pg_index i CROSS JOIN LATERAL "
                "unnest(i.indkey) WITH ORDINALITY k(num,pos) JOIN pg_attribute a "
                "ON a.attrelid=i.indrelid AND a.attnum=k.num WHERE i.indisprimary "
                "AND i.indrelid=?::regclass ORDER BY k.pos",
                (table,),
            ).fetchall()
            if not keys:
                raise AssistantError("snapshot_schema", "备份表缺少主键")
            order = " ORDER BY " + ",".join(_identifier(row[0]) for row in keys)
        large = {
            "media_originals",
            "input_image_blobs",
            "sticker_library",
            "file_library",
            "asset_restore_jobs",
        }
        with db.stream(
            "SELECT * FROM " + name + predicate + order, args, batch=1 if table in large else 16
        ) as cursor:
            yield ["table", table, [col.name for col in cursor.description]]
            for row in cursor:
                yield ["row", [_encode(value) for value in row]]


def fingerprint(store, db=None):
    if db is None:
        with store.db() as tx:
            tx.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return fingerprint(store, tx)
    digest = hashlib.sha256()
    for record in records(store, db, ordered=True):
        digest.update(_raw(record))
        digest.update(b"\n")
    return digest.hexdigest()


def write(stream, records, key):
    identifier = secrets.token_bytes(16)
    cipher = ContentCipher(key)
    stream.write(MAGIC + identifier)
    count = size = 0
    buffer = bytearray()

    def frame(raw):
        nonlocal count
        body = cipher.encrypt(zlib.compress(raw, 1), "backup-frame", identifier.hex(), str(count))
        stream.write(struct.pack(">I", len(body)))
        stream.write(body)
        count += 1

    for record in records:
        line = _raw(record) + b"\n"
        if len(line) > MAX_LINE:
            raise AssistantError("backup_row_limit", "单条备份记录超过上限")
        size += len(line)
        buffer.extend(line)
        offset = 0
        while len(buffer) - offset >= CHUNK:
            frame(buffer[offset : offset + CHUNK])
            offset += CHUNK
        if offset:
            del buffer[:offset]
    if buffer:
        frame(buffer)
    frame(b"")
    return size


def read(stream, key, *, raw_limit=None):
    if stream.read(len(MAGIC)) != MAGIC:
        raise invalid()
    identifier = stream.read(16)
    if len(identifier) != 16:
        raise invalid()
    cipher = ContentCipher(key)
    count = 0
    decoded = 0
    pending = bytearray()
    try:
        while True:
            (length,) = struct.unpack(">I", stream.read(4))
            if not 31 <= length <= CHUNK + 1024:
                raise invalid()
            frame = stream.read(length)
            if len(frame) != length:
                raise invalid()
            compressed = cipher.decrypt(frame, "backup-frame", identifier.hex(), str(count))
            count += 1
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, CHUNK + 1)
            if not decoder.eof or decoder.unused_data or len(raw) > CHUNK:
                raise invalid()
            decoded += len(raw)
            if raw_limit is not None and decoded > raw_limit:
                raise AssistantError("backup_limit", "备份超过此导入入口的容量上限")
            if not raw:
                if pending or stream.read(1):
                    raise invalid()
                return
            parts = raw.split(b"\n")
            for part in parts[:-1]:
                pending.extend(part)
                if len(pending) > MAX_LINE:
                    raise invalid()
                yield json.loads(pending)
                pending.clear()
            pending.extend(parts[-1])
            if len(pending) > MAX_LINE:
                raise invalid()
    except (struct.error, ValueError, zlib.error, UnicodeError):
        raise invalid() from None


class DigestReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def inspect(path, key):
    counts = {}
    with path.open("rb") as handle:
        reader = DigestReader(handle)
        current = None
        for entry in read(reader, key):
            if entry[0] == "table":
                current = entry[1]
                counts[current] = 0
            elif entry[0] == "row":
                counts[current] += 1
        return {"counts": counts, "sha256": reader.digest.hexdigest()}


def create(store, path, key):
    def entries(db):
        yield from records(store, db)
        for item in asset_transfer.iter_assets(store):
            yield ["asset", item]

    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".qiban-backup-", suffix=".tmp", delete=False
    ) as tmp:
        temporary = Path(tmp.name)
    try:
        with temporary.open("wb") as handle, store.db() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if db.execute("SELECT 1 FROM asset_restore_jobs LIMIT 1").fetchone():
                raise AssistantError("restore_pending", "请先完成上次图片恢复")
            size = write(handle, entries(db), key)
            handle.flush()
            os.fsync(handle.fileno())
        result = inspect(temporary, key)
        os.replace(temporary, path)
        return {"raw_bytes": size, "sha256": result["sha256"]}
    finally:
        temporary.unlink(missing_ok=True)


def restore(store, path, key, *, expected, digest, finalize=None):
    from .postgres_transfer import finish_restore

    with store.lock, store.backup_lock, path.open("rb") as handle, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not expected or fingerprint(store, db) != expected:
            raise AssistantError("edit_conflict", "恢复预览后资料已经变化，请重新检查")
        if any((store.state(k, {}, db) or {}).get("status") == "active" for k in RUN_KEYS):
            raise AssistantError("restore_active", "恢复前需要先停止角色")
        # A verified maintenance transaction can replace a table larger than the
        # interactive query timeout. The connection's lock timeout still applies.
        db.execute("SET LOCAL statement_timeout=0")
        reader = DigestReader(handle)
        entries = read(reader, key)
        if next(entries, None) != ["schema", migrations(db)]:
            raise AssistantError("backup_schema", "备份数据库版本不一致")
        local = {k: store.state(k, db=db) for k in LOCAL_STATE_KEYS}
        names = tables(store)
        for table in reversed(names):
            db.execute("DELETE FROM " + _identifier(table))
        item = next(entries, None)
        for table in names:
            name = _identifier(table)
            cols = [c.name for c in db.execute("SELECT * FROM " + name + " LIMIT 0").cursor.description]
            if item != ["table", table, cols]:
                raise AssistantError("backup_schema", "备份字段不匹配")
            sql = "INSERT INTO " + name + (" OVERRIDING SYSTEM VALUE" if table == "content_audit" else "")
            sql += " VALUES(" + ",".join("?" for _ in cols) + ")"
            item = next(entries, None)
            while item and item[0] == "row":
                values = item[1]
                if len(values) != len(cols):
                    raise invalid()
                if table != "state" or values[cols.index("key")] not in LOCAL_STATE_KEYS:
                    db.execute(sql, tuple(_decode(v) for v in values))
                item = next(entries, None)

        def assets():
            nonlocal item
            while item is not None:
                if len(item) != 2 or item[0] != "asset":
                    raise invalid()
                yield item[1]
                item = next(entries, None)
            if reader.digest.hexdigest() != digest:
                raise AssistantError("edit_conflict", "备份文件在预览后已改变")

        for k, value in local.items():
            if value is not None:
                store.set_state(k, value, db)
        finish_restore(store, db, assets(), finalize)
    asset_transfer.finish(store)
    return {"restored": True, "role_started": False, "sessions_revoked": True, "grants_require_review": True}
