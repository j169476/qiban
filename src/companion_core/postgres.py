"""PostgreSQL authoritative record store, with encrypted payloads and CAS writes.

Schema migration/provisioning is explicit. Opening a store never starts a role,
restores a session, imports YAML, or quietly falls back to the old database.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml
from filelock import FileLock

from .cipher import ContentCipher
from .errors import AssistantError
from .pg_connection import Database
from .policy_json import PolicyJSON
from .profiles import default_profile
from .storage import Store, StrictLoader, atomic_write, digest


def migrate(database, profile):
    scripts = sorted((Path(__file__).parent / "sql").glob("[0-9]*.sql"))
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        found = db.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]
        known = dict(db.execute("SELECT version,checksum FROM schema_migrations")) if found else {}
        for path in scripts:
            number = int(path.name.split("_", 1)[0])
            raw = path.read_bytes()
            normalized = raw.replace(b"\r\n", b"\n")
            checksum = hashlib.sha256(normalized).hexdigest()
            if number in known:
                # Earlier Windows checkouts hashed CRLF. Accept that historical
                # representation without rewriting its audit record; changed SQL
                # still fails, while future migrations use a stable LF digest.
                equivalent = {checksum, hashlib.sha256(raw).hexdigest(),
                              hashlib.sha256(normalized.replace(b"\n", b"\r\n")).hexdigest()}
                if known[number] not in equivalent:
                    raise AssistantError("migration_checksum", "已执行数据库升级文件发生变化，拒绝继续")
                continue
            db.executescript(raw.decode("utf-8"))
            db.execute("INSERT INTO schema_migrations(version,checksum) VALUES(?,?)", (number, checksum))
        for kind, schema in profile.schemas.items():
            db.execute("""INSERT INTO record_kinds VALUES(?,?::jsonb,?)
                       ON CONFLICT(kind) DO UPDATE SET schema_json=excluded.schema_json,
                       is_collection=excluded.is_collection""",
                       (kind, json.dumps(schema), kind in profile.collections))


class PostgresStore(Store):
    backend = "postgres"

    def __init__(self, root, connection_options, key, profile=None):
        self.profile = profile or default_profile()
        self.root = Path(root).resolve()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.runtime / "write.lock"), timeout=20)
        self.database = Database(connection_options)
        self.cipher = ContentCipher(key)
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        self._backup_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                               info=b"companion-postgres-backup-v1").derive(key)
        self._read_cache, self._index_local = {}, threading.local()
        self.index_stats = {"memory_hits": 0, "persistent_hits": 0, "parsed": 0, "cache_errors": 0}
        # No file database path: callers that have not been ported must fail visibly.
        self.db_path = None
        # Trusted service memory only. No SQLite/plaintext disk projection; ACL
        # filtering precedes cache access. Include ciphertext digest to detect a
        # changed/tampered body even when its declared content version is unchanged.
        self._decoded = OrderedDict()
        self._decoded_bytes = 0
        self._decoded_lock = threading.RLock()
        self._decoded_limit = 128 * 1024 * 1024
        self._policy_json = PolicyJSON()

    def db(self):
        return self.database.connect()

    def migrate(self):
        migrate(self.database, self.profile)
        from .conversation_index import backfill

        backfill(self)
        from .fact_index import backfill as backfill_facts

        backfill_facts(self)
        if self.profile.migrator:
            self.profile.migrator(self)
        return {"migrated": True, "backend": self.backend}

    def _key(self, kind, item_id=None):
        # Reuse the existing strict kind/ID validation, not the resulting filename.
        super().path(kind, item_id)
        return kind, item_id or "@config"

    def exists(self, kind, item_id=None):
        key = self._key(kind, item_id)
        with self.db() as db:
            return db.execute("SELECT 1 FROM content_records WHERE kind=? AND id=?", key).fetchone() is not None

    def version(self, kind, item_id=None):
        key = self._key(kind, item_id)
        with self.db() as db:
            row = db.execute("SELECT version FROM content_records WHERE kind=? AND id=?", key).fetchone()
        return row[0] if row else None

    def initialize(self):
        # Explicit migration must already have succeeded with the migration owner.
        if self.state("governance-epoch") is None:
            self.set_state("governance-epoch", secrets.randbelow(2**31))
        for kind, original in self.profile.defaults.items():
            if not self.exists(kind):
                data = deepcopy(original)
                if kind == "manifest":
                    import uuid
                    data["project_id"] = uuid.uuid4().hex
                self.write(kind, data, None, "initialize")
        for folder in ("cache", "assets", "history", "conflicts"):
            (self.root / folder).mkdir(exist_ok=True)
        return self.validate_all()

    def _decode(self, kind, item_id, version, body):
        encrypted = bytes(body)
        cache_key = kind, item_id, version, digest(encrypted)
        with self._decoded_lock:
            cached = self._decoded.get(cache_key)
            if cached is not None:
                self._decoded.move_to_end(cache_key)
                self.index_stats["memory_hits"] += 1
                return deepcopy(cached[0])
        raw = self.cipher.decrypt(encrypted, kind, item_id, version)
        if len(raw) > 1_000_000 or digest(raw) != version:
            raise AssistantError("storage_integrity", "资料内容摘要不一致")
        try:
            data = yaml.load(raw.decode("utf-8"), Loader=StrictLoader)
        except (ValueError, UnicodeError, yaml.YAMLError):
            raise AssistantError("invalid_data", "数据库资料格式无效") from None
        self.profile.validate(kind, data)
        if item_id != "@config" and data["id"] != item_id:
            raise AssistantError("id_mismatch", "数据库资料归属不一致")
        result = {"data": data, "version": version}
        charge = len(raw) * 6 + 1024
        with self._decoded_lock:
            if charge < self._decoded_limit and cache_key not in self._decoded:
                self._decoded[cache_key] = (deepcopy(result), charge)
                self._decoded_bytes += charge
                while self._decoded_bytes > self._decoded_limit:
                    _, (_, removed) = self._decoded.popitem(last=False)
                    self._decoded_bytes -= removed
        self.index_stats["parsed"] += 1
        return result

    def read(self, kind, item_id=None):
        key = self._key(kind, item_id)
        with self.db() as db:
            row = db.execute("SELECT version,body FROM content_records WHERE kind=? AND id=?", key).fetchone()
        if row is None:
            raise AssistantError("not_found", "资料不存在")
        return self._decode(*key, row["version"], row["body"])

    def write(self, kind, data, expected, actor, item_id=None, allow_locked=False):
        self.profile.validate(kind, data)
        self._key(kind, item_id)
        if item_id is not None and data["id"] != item_id:
            raise AssistantError("id_mismatch", "数据库条目 ID 不一致")
        raw = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")
        return self._write_raw(kind, item_id, data, raw, expected, actor, allow_locked=allow_locked)

    def _write_raw(self, kind, item_id, data, raw, expected, actor, *, allow_locked=False):
        key = self._key(kind, item_id)
        if len(raw) > 1_000_000:
            raise AssistantError("invalid_data", "资料超过大小上限")
        version = digest(raw)
        encrypted = self.cipher.encrypt(raw, *key, version)
        source = data.get("source", {})
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            from .baseline import guard_write

            guard_write(self, kind, item_id, data)
            old = db.execute("SELECT version,body,locked,revision FROM content_records WHERE kind=? AND id=? FOR UPDATE",
                             key).fetchone()
            actual = old["version"] if old else None
            if actual != expected:
                raise AssistantError("edit_conflict", "资料已由另一入口修改，请重新读取")
            if old and old["locked"] and not allow_locked:
                raise AssistantError("locked", "该条目已锁定")
            if old:
                db.execute("INSERT INTO content_history(kind,id,version,body) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                           (*key, old["version"], old["body"]))
            db.execute("""INSERT INTO content_records(kind,id,version,revision,body,locked,source_type,source_ref)
                       VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET version=excluded.version,
                       revision=excluded.revision,body=excluded.body,locked=excluded.locked,
                       source_type=excluded.source_type,source_ref=excluded.source_ref,updated_at=now()""",
                       (*key, version, old["revision"] + 1 if old else 1, encrypted, bool(data.get("locked")),
                        source.get("type", ""), source.get("reference", "")))
            from .conversation_index import write as index_conversation

            index_conversation(db, kind, data)
            from .fact_index import write as index_fact

            index_fact(self, db, kind, data, version)
            self.audit("config" if kind in self.profile.config else "content",
                       {"actor": actor, "resource": kind + "/" + key[1], "before": actual, "after": version})
            from .access import RecordPolicy
            from .governance import GovernanceRepository, resource_id

            repository = GovernanceRepository(self)
            resource = resource_id(kind, item_id)
            policy = repository.policy(resource)
            if old is None and policy is not None:
                from .provenance import sync_scope

                # Recreating a deleted aggregate is not continuous growth. Its
                # previous historical evidence must not become readable again.
                sync_scope(repository, policy, policy, db, force=True)
            next_policy = (replace(policy, version=secrets.token_hex(16)) if policy else
                           RecordPolicy(resource, secrets.token_hex(16)))
            repository.set_policy(next_policy, policy.version if policy else None, actor, db=db)
        return {"data": deepcopy(data), "version": version}

    def records(self, kind):
        return self.records_selected(kind)

    def records_selected(self, kind, ids=None):
        if kind not in self.profile.collections:
            raise AssistantError("invalid_path", "未知集合")
        result, issues = [], []
        with self.db() as db:
            rows = db.execute("SELECT id,version,body FROM content_records WHERE kind=?"
                              + (" AND id=ANY(?)" if ids is not None else "") + " ORDER BY id",
                              (kind, list(ids)) if ids is not None else (kind,)).fetchall()
        for row in rows:
            try:
                result.append(self._decode(kind, row["id"], row["version"], row["body"]))
            except AssistantError as exc:
                issues.append({"file": kind + "/" + row["id"], **exc.as_dict()})
        return result, issues

    def set_state(self, key, value, db=None):
        if db is None:
            with self.db() as connection:
                return self.set_state(key, value, connection)
        if key == "public-growth":
            from .governance import GovernanceRepository
            from .provenance import invalidate_state

            db.execute("BEGIN IMMEDIATE")
            invalidate_state(GovernanceRepository(self), db)
        db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, self.ledger_encode("state:" + key, value)))
        if key.startswith("core-processed:"):
            db.execute("""INSERT INTO core_event_receipts VALUES(?,?) ON CONFLICT(event_id)
                       DO UPDATE SET version=excluded.version""", (key[15:], value))
        elif key.startswith("core-delivery:"):
            db.execute("""INSERT INTO core_delivery_state VALUES(?,?,?,?,?,?) ON CONFLICT(event_id)
                       DO UPDATE SET version=excluded.version,owner=excluded.owner,status=excluded.status,
                       due_at=excluded.due_at,config_token=excluded.config_token""",
                       (key[14:], value["version"], value["owner"], value["status"],
                        value.get("due_at"), value.get("config_token")))

    def ledger_encode(self, domain, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 2_000_000:
            raise AssistantError("invalid_data", "运行记录超过大小上限")
        return "enc1:" + base64.b64encode(self.cipher.encrypt(raw, "ledger", domain, "v1")).decode("ascii")

    def ledger_decode(self, domain, value):
        if not isinstance(value, str) or not value.startswith("enc1:"):
            raise AssistantError("storage_integrity", "运行记录不是有效的加密载荷")
        try:
            raw = base64.b64decode(value[5:], validate=True)
            return json.loads(self.cipher.decrypt(raw, "ledger", domain, "v1"))
        except (ValueError, UnicodeError):
            raise AssistantError("storage_integrity", "运行记录完整性校验失败") from None

    def audit(self, category, event):
        # Audit stores identifiers and revisions, never a duplicate content body.
        metadata = {key: value for key, value in event.items() if key in ("event", "error", "choice", "status")}
        with self.db() as db:
            db.execute("""INSERT INTO content_audit(category,actor,resource,before_version,after_version,metadata)
                       VALUES(?,?,?,?,?,?::jsonb)""",
                       (category, str(event.get("actor", "system")), str(event.get("resource", event.get("file", ""))),
                        event.get("before"), event.get("after"), json.dumps(metadata)))

    def backup(self, force=False):
        from .postgres_transfer import create, unpack

        folder = self.root / "history" / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = folder / (day + ("-" + secrets.token_hex(4) if force else "") + ".ccbackup")
        with self.lock:
            reused = False
            metadata = {}
            if target.is_file() and not target.is_symlink():
                try:
                    unpack(target.read_bytes(), self._backup_key)
                    reused = True
                except AssistantError:
                    pass
            if not reused:
                blob = create(self, self._backup_key, metadata=metadata)
                unpack(blob, self._backup_key)
                atomic_write(target, blob)
            keep = self.read("policy")["data"]["backup_keep"]
            older = sorted((path for path in folder.glob("*.ccbackup") if path != target),
                           key=lambda path: path.stat().st_mtime_ns, reverse=True)
            for old in older[max(0, keep - 1):]:
                if old.resolve().is_relative_to(folder.resolve()) and not old.is_symlink():
                    old.unlink()
        return {"path": str(target), "sha256": digest(target.read_bytes()), "encrypted": True,
                "reused": reused, **metadata}

    def checkpoint(self):
        return self.backup()

    def validate_critical(self):
        result = super().validate_critical()
        with self.db() as db:
            pending = db.execute("SELECT COUNT(*) FROM asset_restore_jobs").fetchone()[0]
        if pending:
            result["issues"].append({"critical": True, "error": "restore_pending", "message": "图片恢复尚未完成"})
            result["ok"] = False
        return result

    def reindex(self):
        # PostgreSQL maintains relational indexes transactionally. Authorized
        # lexical passages are built in memory and never projected to SQLite.
        return {"backend": "postgres", "transactional_indexes": True, "plaintext_cache_written": False}

    def export(self, destination):
        raise AssistantError("controlled_export_required", "数据库资料需要通过已认证管理入口导出")
