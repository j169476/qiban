"""Per-project PostgreSQL process; no Windows service or machine startup changes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from filelock import FileLock

from companion_core.errors import AssistantError


def command(root, *arguments, timeout=45):
    root = Path(root).resolve()
    binary = root / "pgsql" / "bin" / "pg_ctl.exe"
    if not binary.is_file() or not (root / "database" / "PG_VERSION").is_file():
        raise AssistantError("database_runtime", "托管数据库目录不完整")
    with (root / "control.log").open("ab") as log:
        # Files, not pipes: PostgreSQL children retain inherited descriptors.
        process = subprocess.Popen([str(binary), "-D", str(root / "database"), *arguments], stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            raise AssistantError("database_timeout", "数据库进程未在限定时间内就绪") from None


def ensure(config):
    root = config.get("postgres_runtime")
    if not root:
        return  # Explicit externally managed database, used by isolated tests.
    path = Path(root).resolve()
    expected = Path(os.environ.get("LOCALAPPDATA", "")) / "Companion" / config["id"]
    if path != expected.resolve():
        raise AssistantError("database_runtime", "托管数据库不属于当前安装")
    with FileLock(str(path / "postgres-start.lock"), timeout=45):
        if command(path, "status") == 0:
            return
        if command(path, "-l", str(path / "database.log"), "-w", "start") != 0:
            raise AssistantError("database_unavailable", "本机数据库未能启动，角色保持暂停")
