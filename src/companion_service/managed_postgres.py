"""Per-project PostgreSQL process; no Windows service or machine startup changes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from filelock import FileLock, Timeout

from companion_core.errors import AssistantError
from companion_core.operation_context import bounded, remaining, stage


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
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            raise AssistantError("database_timeout", "数据库进程未在限定时间内就绪") from None


def ensure(config):
    root = config.get("postgres_runtime")
    if not root:
        return  # Explicit externally managed database, used by isolated tests.
    path = Path(root).resolve()
    expected = Path(os.environ.get("LOCALAPPDATA", "")) / "Companion" / config["id"]
    if path != expected.resolve():
        raise AssistantError("database_runtime", "托管数据库不属于当前安装")
    try:
        with bounded(50, "database_timeout"), stage("database_lock"), FileLock(str(path / "postgres-start.lock"), timeout=remaining(5)):
            with stage("database_status"):
                if command(path, "status", timeout=remaining(5)) == 0:
                    return
            with stage("database_recovery"):
                # PostgreSQL performs WAL recovery itself; never delete a live PID file.
                if command(path, "-l", str(path / "database.log"), "-w", "-t", "40", "start", timeout=remaining(43)) != 0:
                    raise AssistantError("database_unavailable", "本机数据库未能启动，请查看数据库恢复阶段诊断")
    except Timeout:
        raise AssistantError("database_start_busy", "数据库正在由另一启动请求恢复，请稍后查询状态") from None
