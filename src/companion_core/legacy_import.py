"""Read-only, bounded legacy capture and atomic import into a fresh database.

Opening an old data space here never constructs Store, recovers a journal, or
changes the original SQLite/YAML. Unknown ownership always stays quarantined.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from pathlib import Path

import yaml
from filelock import FileLock

from companion_core.runtime_keys import RUN_KEYS

from .errors import AssistantError
from .storage import StrictLoader, digest

ENCRYPTED = {"actions": ("payload", "result"), "core_actions": ("body", "result"),
             "events": ("body",), "core_events": ("body",), "relationship_pending": ("data", "after_yaml")}


def capture(root, profile):
    root = Path(root).resolve()
    with FileLock(str(root / "runtime" / "write.lock"), timeout=20):
        return _capture(root, profile)


def _capture(root, profile):
    records, raw_total = [], 0
    for kind in profile.schemas:
        paths = sorted((root / kind).glob("*.yaml")) if kind in profile.collections else [root / (kind + ".yaml")]
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                raise AssistantError("migration_source", "原资料缺失或路径不在资料目录内")
            raw = path.read_bytes()
            raw_total += len(raw)
            if len(raw) > 1_000_000 or raw_total > 512 * 1024 * 1024:
                raise AssistantError("migration_limit", "原资料超过迁移大小上限")
            try:
                data = yaml.load(raw.decode("utf-8-sig"), Loader=StrictLoader)
            except (UnicodeError, yaml.YAMLError):
                raise AssistantError("migration_source", "原资料格式无效") from None
            profile.validate(kind, data)
            item_id = path.stem if kind in profile.collections else None
            if item_id is not None and (not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item_id) or data["id"] != item_id):
                raise AssistantError("migration_source", "原资料 ID 不一致")
            records.append({"kind": kind, "id": item_id, "raw": raw, "data": data, "version": digest(raw), "path": path})
    database = root / "runtime" / "state.sqlite3"
    if not database.is_file():
        # Older distributions used state.sqlite; require the one exact known name.
        database = root / "runtime" / "state.sqlite"
    if not database.is_file():
        raise AssistantError("migration_source", "找不到原运行账本")
    tables = {}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(profile.tables):
            if not re.fullmatch(r"[a-z_]+", table) or table not in present:
                raise AssistantError("migration_source", "原运行账本表结构不完整")
            cursor = db.execute('SELECT * FROM "' + table + '"')
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor]
            tables[table] = {"columns": columns, "rows": rows}
        if tables["relationship_pending"]["rows"]:
            raise AssistantError("migration_pending", "原资料有待裁决的关系提交，请先完成恢复")
        for row in tables["state"]["rows"]:
            if row["key"] in RUN_KEYS and (json.loads(row["value"]) or {}).get("status") == "active":
                raise AssistantError("migration_active", "迁移前需要停止原角色运行")
    if any(digest(record["path"].read_bytes()) != record["version"] for record in records):
        raise AssistantError("migration_changed", "读取期间原资料已变化")
    signature = hashlib.sha256(json.dumps({"records": [(r["kind"], r["id"], r["version"]) for r in records],
                                          "tables": tables}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    from .asset_transfer import capture as capture_assets

    class Source:
        def __init__(self):
            self.root, self.profile = root, profile

        def read(self, kind):
            return next(item for item in records if item["kind"] == kind and item["id"] is None)

        def records(self, kind):
            return [item for item in records if item["kind"] == kind], []

    return {"records": records, "tables": tables, "fingerprint": signature, "assets": capture_assets(Source())}


def apply(store, snapshot):
    """Import only a captured source; no production source path is changed here."""
    from .asset_transfer import finish

    result = _apply_database(store, snapshot)
    finish(store)
    return result


def _apply_database(store, snapshot):
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = store.state("legacy-import", db=db)
        if previous:
            if previous["fingerprint"] != snapshot["fingerprint"]:
                raise AssistantError("migration_conflict", "本数据库已导入另一份资料")
            return {**previous, "reused": True}
        if db.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]:
            raise AssistantError("migration_not_empty", "迁移目标需要是新建且未初始化资料的数据库")
        for item in snapshot["records"]:
            store.profile.validate(item["kind"], item["data"])
            store._write_raw(item["kind"], item["id"], item["data"], item["raw"], None, "legacy-import")
        for table, block in snapshot["tables"].items():
            if table not in store.profile.tables or not re.fullmatch(r"[a-z_]+", table):
                raise AssistantError("migration_source", "迁移包含未声明的账本")
            if table == "state":
                for row in block["rows"]:
                    store.set_state(row["key"], json.loads(row["value"]), db)
                continue
            columns = block["columns"]
            if any(not re.fullmatch(r"[a-z_]+", name) for name in columns):
                raise AssistantError("migration_source", "迁移字段无效")
            query = "INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")"
            values = []
            for original in block["rows"]:
                row = dict(original)
                for column in ENCRYPTED.get(table, ()):
                    if row[column] is not None:
                        value = row[column] if column == "after_yaml" else json.loads(row[column])
                        row[column] = store.ledger_encode(table + "." + column, value)
                if table in ("actions", "core_actions"):
                    if row["status"] == "prepared":
                        row["status"] = "cancelled"
                    elif row["status"] == "dispatching":
                        row["status"] = "uncertain"
                values.append(tuple(row[column] for column in columns))
            db.executemany(query, values)
        store.set_state("governance-epoch", secrets.randbits(62), db)
        for key in RUN_KEYS:
            value = store.state(key, {}, db)
            if value:
                store.set_state(key, {**value, "status": "paused"}, db)
        counts = {kind: 0 for kind in store.profile.schemas}
        for item in snapshot["records"]:
            counts[item["kind"]] += 1
            if store.version(item["kind"], item["id"]) != item["version"]:
                raise AssistantError("migration_verification", "迁移后版本不一致")
        validation = store.validate_all()
        if not validation["ok"]:
            raise AssistantError("migration_verification", "迁移后数据校验失败")
        result = {"fingerprint": snapshot["fingerprint"], "counts": counts,
                  "ledger_counts": {table: len(block["rows"]) for table, block in snapshot["tables"].items()},
                  "quarantined": len(snapshot["records"]), "role_started": False}
        if snapshot.get("selection"):
            from .seed_import import stage_owner_claim

            result["selection"] = snapshot["selection"]
            stage_owner_claim(store, snapshot["selection"], db)
        store.set_state("legacy-import", result, db)
        from .asset_transfer import enqueue

        enqueue(store, snapshot.get("assets", []), db)
        return result
