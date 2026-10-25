"""Explicit, paused cutover from legacy files to a dedicated PostgreSQL cluster."""
from __future__ import annotations

import base64
import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

import keyring
import psycopg

from bili_assistant.profiles import profile
from companion_core.errors import AssistantError
from companion_core.legacy_import import apply, capture
from companion_core.pg_roles import provision
from companion_core.postgres import PostgresStore
from companion_core.storage import atomic_write

from .config import VAULT, metadata, save
from .managed_postgres import command


def install(project, bundle, **kwargs):
    from filelock import FileLock

    from .installation import Journal

    project = Path(project).resolve()
    journal = Journal(project)
    with FileLock(str(project / ".companion-install.lock"), timeout=0):
        if journal.read() is not None:
            raise AssistantError("installation_pending", "已有安装记录，请先查看 install-status")
        try:
            return _install(project, bundle, journal=journal, **kwargs)
        except BaseException as exc:
            journal.failed(exc.code if isinstance(exc, AssistantError) else "installation_interrupted")
            raise


def _install(project, bundle, *, journal, admin_port=45180, role_port=45181, recovery=None, seed_only=False):
    project, bundle = Path(project).resolve(), Path(bundle).resolve()
    if os.name != "nt" or metadata(project) is not None:
        raise AssistantError("installation_scope", "首次安装需要 Windows 本机且项目尚未迁移")
    if not all((bundle / part).is_dir() for part in ("bin", "lib", "share")):
        raise AssistantError("postgres_bundle", "请提供已核对来源的 PostgreSQL Windows 二进制目录")
    for port in (admin_port, role_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    contract = profile()
    source = project / "bili-assistant-data"
    snapshot = None
    if recovery is not None and seed_only:
        raise AssistantError("installation_scope", "完整恢复不能同时选择基础资料迁移")
    if recovery is None:
        snapshot = capture(source, contract)
        if seed_only:
            from companion_core.seed_import import select

            snapshot = select(snapshot, contract, quota_tables=("videos",))
        data_key = secrets.token_bytes(32)
    else:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        from companion_core.postgres_transfer import unpack

        if source.exists():
            raise AssistantError("recovery_destination", "灾难恢复必须使用没有旧数据的新项目目录")
        blob, data_key = recovery
        backup_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                          info=b"companion-postgres-backup-v1").derive(data_key)
        unpack(blob, backup_key)  # Authenticate before creating a cluster or vault entry.
    identifier = uuid.uuid4().hex
    runtime = Path(os.environ["LOCALAPPDATA"]) / "Companion" / identifier
    if not str(runtime).isascii():
        raise AssistantError("postgres_path", "当前 Windows PostgreSQL 要求 ASCII 运行目录，请先配置外部受限数据库")
    root = project / "companion-service-data"
    if root.exists() or runtime.exists():
        raise AssistantError("installation_exists", "目标目录已存在，不能覆盖")
    source_fingerprint = snapshot.get("source_fingerprint", snapshot["fingerprint"]) if snapshot else None
    journal.begin(identifier, source_fingerprint)
    credentials = {"data_key": base64.b64encode(data_key).decode(),
                   "database_password": secrets.token_urlsafe(36), "owner_password": secrets.token_urlsafe(36),
                   "bridge_secret": secrets.token_urlsafe(36)}
    # Persist recovery material before creating a database. It never enters the
    # installer result, command line, project config or role model context.
    keyring.set_password(VAULT, identifier, json.dumps(credentials))
    runtime.mkdir(parents=True)
    root.mkdir()
    for protected in (runtime, root):
        permission = subprocess.run(["icacls.exe", str(protected), "/inheritance:r", "/grant:r",
            getpass.getuser() + ":(OI)(CI)F", "SYSTEM:(OI)(CI)F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if permission.returncode:
            raise AssistantError("directory_permissions", "无法限制服务资料目录权限，安装中止")
    journal.advance("directories")
    for part in ("bin", "lib", "share"):
        shutil.copytree(bundle / part, runtime / "pgsql" / part)
    journal.advance("copied")
    pw = runtime / "init-password"
    atomic_write(pw, credentials["owner_password"].encode())
    try:
        with (runtime / "install.log").open("ab") as output:
            run = subprocess.run([str(runtime / "pgsql/bin/initdb.exe"), "-D", str(runtime / "database"),
                "-U", "companion_owner", "--pwfile=" + str(pw), "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"],
                stdout=output, stderr=output, stdin=subprocess.DEVNULL, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
            if run.returncode:
                raise AssistantError("database_init", "数据库初始化失败；保留现场，不切换原资料")
    finally:
        pw.unlink(missing_ok=True)
    journal.advance("initialized")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (runtime / "database/postgresql.conf").open("a", encoding="utf-8") as f:
        f.write(f"\nlisten_addresses='127.0.0.1'\nport={port}\nlog_statement='none'\nlog_min_error_statement='panic'\n")
    if command(runtime, "-l", str(runtime / "database.log"), "-w", "start"):
        raise AssistantError("database_init", "数据库启动失败；原资料未切换")
    journal.advance("database_started")
    owner_options = {"host": "127.0.0.1", "port": port, "user": "companion_owner", "password": credentials["owner_password"], "dbname": "postgres"}
    with psycopg.connect(**owner_options, autocommit=True) as db:
        db.execute("CREATE DATABASE companion")
    owner_options["dbname"] = "companion"
    with psycopg.connect(**owner_options) as db:
        db.execute("REVOKE ALL ON DATABASE companion FROM PUBLIC")
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        db.execute("CREATE SCHEMA companion")
    store = PostgresStore(root, {**owner_options, "options": "-c search_path=companion"}, base64.b64decode(credentials["data_key"]), contract)
    try:
        store.migrate()
        if recovery is None:
            imported = apply(store, snapshot)
            if capture(source, contract)["fingerprint"] != source_fingerprint:
                raise AssistantError("source_changed", "切换前旧资料发生变化，安装中止，未修改旧目录")
        else:
            from companion_core.postgres_transfer import fingerprint, restore

            store.initialize()
            imported = restore(store, blob, store._backup_key, expected=fingerprint(store))
        backup = store.backup(force=True)
        journal.advance("imported")
        with psycopg.connect(**owner_options) as db:
            provision(db, schema="companion", role="companion_app", password=credentials["database_password"])
            db.execute("GRANT CONNECT ON DATABASE companion TO companion_app")
        config = {"version": 1, "id": identifier, "project": str(project), "data": str(root), "postgres_runtime": str(runtime),
                  "ports": {"admin": admin_port, "role": role_port}, "database": {"host": "127.0.0.1", "port": port,
                  "dbname": "companion", "user": "companion_app", "options": "-c search_path=companion"}}
        check = PostgresStore(root, {**config["database"], "password": credentials["database_password"]}, base64.b64decode(credentials["data_key"]), contract)
        try:
            if not check.validate_critical()["ok"]:
                raise AssistantError("installation_verification", "受限数据库账号校验失败，未切换")
        finally:
            check.database.close()
        from companion_core.postgres_transfer import fingerprint

        journal.advance("verified", config=config, target_fingerprint=fingerprint(store))
        # Commit point: afterwards all CLI operations fail closed onto the service.
        save(project, config, credentials)
        if recovery is None:
            atomic_write(source / ".service-retired.json", json.dumps({"service": identifier, "source_fingerprint": source_fingerprint}).encode())
        journal.advance("committed")
        return {"installed": True, "role_started": False, "source_files_preserved": True, "import": imported,
                "backup": backup, "setup": "从后台管理聊天调用 service_setup 建立网页密码"}
    finally:
        store.database.close()
