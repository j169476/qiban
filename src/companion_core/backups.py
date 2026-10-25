"""Verified full backups and staged restoration to a new, paused data directory."""

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from .errors import AssistantError
from .profiles import BASE_TABLES, default_profile
from .storage import Store, utcnow

FORMAT = "bili-assistant.full-backup"
MANIFEST = "backup-manifest.json"
MAX_TOTAL = 2_000_000_000
MAX_FILE = 512_000_000


def sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def allowed(name, profile=None):
    profile = profile or default_profile()
    parts = PurePosixPath(name).parts
    if "\\" in name or ":" in name or not parts or ".." in parts or name.startswith("/"):
        return False
    if name in (MANIFEST, "runtime/state.sqlite3", *(f"{kind}.yaml" for kind in (*profile.config, "growth"))):
        return True
    if len(parts) == 2 and parts[0] in profile.collections:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.yaml", parts[1]))
    return (len(parts) == 3 and parts[:2] in (("assets", "persona"), ("assets", "publication"))
            and bool(re.fullmatch(r"[A-Za-z0-9_-]+\.(?:png|jpg|webp)", parts[2])))


def create(store, destination):
    from .relationship_store import pending, recover

    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with store.lock, tempfile.TemporaryDirectory(prefix=".backup-", dir=destination.parent) as folder:
        recover(store)
        if pending(store) or not store.validate_all()["ok"]:
            raise AssistantError("backup_invalid_state", "先解决无效文件或关系提交冲突，再创建完整备份")
        stage = Path(folder)
        content = stage / "content.zip"
        exported = store.export(content)
        if exported["missing_assets"]:
            raise AssistantError("backup_missing_assets", "完整备份需要所有已注册参考图与发布图片")
        database = stage / "state.sqlite3"
        with store.db() as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AssistantError("backup_database_invalid", "运行数据库校验失败")
        files = {}
        result = stage / "complete.zip"
        with zipfile.ZipFile(content) as source, zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as out:
            for item in source.infolist():
                data = source.read(item)
                files[item.filename] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                out.writestr(item.filename, data)
            files["runtime/state.sqlite3"] = {"size": database.stat().st_size, "sha256": sha(database)}
            if any(v["size"] > MAX_FILE for v in files.values()) or sum(v["size"] for v in files.values()) > MAX_TOTAL:
                raise AssistantError("backup_too_large", "完整备份超过当前 2 GB 总量或 512 MB 单文件上限")
            out.write(database, "runtime/state.sqlite3")
            manifest = {"format": FORMAT, "version": 1, "created_at": utcnow(), "files": files,
                        "credentials_included": False, "restore_requires_explicit_start": True}
            out.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        with result.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(result, destination)
    return {"path": str(destination), "kind": "full", "sha256": sha(destination),
            "files": len(files), "credentials_included": False}


def pause_database(path, profile=None):
    profile = profile or default_profile()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AssistantError("restore_database_invalid", "备份数据库未通过完整性检查")
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = profile.tables
        present = {n for _, n in objects}
        # Old backups predate core tables; Store's additive migration creates them on restore.
        if any(kind != "table" or name not in tables for kind, name in objects) or not (BASE_TABLES | profile.extra_tables) <= present:
            raise AssistantError("restore_database_invalid", "备份数据库结构不受支持")
        if db.execute("SELECT COUNT(*) FROM relationship_pending").fetchone()[0]:
            raise AssistantError("restore_pending_commit", "完整备份含未完成关系提交，不能静默恢复")
        row = db.execute("SELECT value FROM state WHERE key='run'").fetchone()
        if row and json.loads(row[0]):
            run = json.loads(row[0])
            run.update(status="paused", reason="backup_restored", ended_at=utcnow(),
                       generation=uuid.uuid4().hex, background_requested=False)
            db.execute("UPDATE state SET value=? WHERE key='run'", (json.dumps(run, ensure_ascii=False),))
        db.execute("UPDATE actions SET status='cancelled' WHERE status='prepared'")
        db.execute("UPDATE actions SET status='uncertain',result=? WHERE status='dispatching'",
                   (json.dumps({"error": "restore_interrupted", "message": "恢复时结果不明，禁止自动重发"}),))
        if "core_actions" in present:
            db.execute("UPDATE core_actions SET status='cancelled' WHERE status='prepared'")
            db.execute("UPDATE core_actions SET status='uncertain' WHERE status='dispatching'")
        if "core_observations" in present:
            db.execute("UPDATE core_observations SET status='interrupted',error_code='backup_restored' "
                       "WHERE status='running'")
        from .runtime_keys import RUN_KEYS

        for key in RUN_KEYS[1:]:
            core = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            if core:
                value = json.loads(core[0])
                value.update(status="paused", generation=uuid.uuid4().hex)
                db.execute("UPDATE state SET value=? WHERE key=?", (json.dumps(value), key))
        rows = db.execute("SELECT key,value FROM state").fetchall()
        for key, value in rows:
            if key in ("daemon_generation", "notifier", "browser-login:current") or key.startswith(
                ("health:", "supervisor:", "credential-health:", "inbox:")
            ):
                db.execute("DELETE FROM state WHERE key=?", (key,))
            elif key.startswith("fault:"):
                fault = json.loads(value)
                fault["notification"] = "cancelled_on_restore"
                db.execute("UPDATE state SET value=? WHERE key=?", (json.dumps(fault), key))


def restore(archive, destination, *, profile=None, store_factory=None, pause=None):
    profile = profile or default_profile()
    store_factory = store_factory or (lambda path: Store(path, profile))
    pause = pause or (lambda path: pause_database(path, profile))
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    if destination.exists():
        raise AssistantError("restore_destination_exists", "恢复目标必须是尚不存在的新目录，原数据不会被覆盖")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as source:
            items = source.infolist()
            names = [item.filename for item in items]
            if MANIFEST not in names:
                raise AssistantError("restore_content_only", "这是旧版内容导出，缺少完整去重及运行状态，不能冒充完整恢复")
            if (len(items) > 200000 or len({n.casefold() for n in names}) != len(names)
                    or any(not allowed(n, profile) for n in names)
                    or any(stat.S_ISLNK(i.external_attr >> 16) or i.file_size > MAX_FILE for i in items)
                    or sum(i.file_size for i in items) > MAX_TOTAL
                    or source.getinfo(MANIFEST).file_size > 16_000_000):
                raise AssistantError("restore_archive_invalid", "备份路径、大小或条目清单无效")
            manifest = json.loads(source.read(MANIFEST))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
                raise AssistantError("restore_archive_invalid", "完整备份清单无效")
            files = manifest.get("files", {})
            if (manifest.get("format") != FORMAT or manifest.get("version") != 1
                    or set(files) != set(names) - {MANIFEST}
                    or not {"runtime/state.sqlite3", *(f"{k}.yaml" for k in (*profile.config, "growth"))} <= set(files)):
                raise AssistantError("restore_archive_invalid", "完整备份格式或必需文件不匹配")
            with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as folder:
                stage = Path(folder).resolve() / "data"
                stage.mkdir()
                for name, expected in files.items():
                    target = (stage / name).resolve()
                    if not target.is_relative_to(stage):
                        raise AssistantError("restore_archive_invalid", "备份路径超出恢复目录")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with source.open(name) as incoming, target.open("wb") as out:
                        while block := incoming.read(1024 * 1024):
                            out.write(block)
                            digest.update(block)
                            size += len(block)
                        out.flush()
                        os.fsync(out.fileno())
                    if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                        raise AssistantError("restore_checksum", "备份内容与校验清单不一致")
                pause(stage / "runtime/state.sqlite3")
                restored = store_factory(stage)
                validation = restored.initialize()
                if not validation["ok"]:
                    raise AssistantError("restore_invalid_data", "恢复文件未通过结构校验，未启用目标目录")
                if restored.read("persona")["data"].get("visual_identity"):
                    from .persona import resolve_visuals

                    resolve_visuals(restored)
                # Validate all registered assets, including publication image hashes.
                check = restored.export(Path(folder) / "verify.zip")
                if check["missing_assets"]:
                    raise AssistantError("restore_missing_assets", "备份注册图片缺失或已损坏")
                if stage.parent.parent != destination.parent or destination.exists():
                    raise AssistantError("restore_destination_exists", "恢复目标发生变化")
                os.replace(stage, destination)
        return {"restored": True, "path": str(destination), "status": "paused",
                "activated": False, "credentials_restored": False, "counts": validation["counts"]}
    except AssistantError:
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, zipfile.BadZipFile):
        raise AssistantError("restore_failed", "备份读取或校验失败；原数据及目标目录未被替换") from None


def verify(archive, *, profile=None):
    """Exercise the complete restore validator in a disposable directory; never activate it."""
    archive = Path(archive).resolve()
    signature = sha(archive)
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=archive.parent) as folder:
        result = restore(archive, Path(folder) / "data", profile=profile)
    if sha(archive) != signature:
        raise AssistantError("backup_changed", "校验期间备份文件发生变化")
    return {"path": str(archive), "kind": "full", "sha256": signature,
            "verified": True, "counts": result["counts"], "credentials_included": False}
