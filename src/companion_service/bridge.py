"""Trusted CLI bridge with bounded startup and sanitized progress diagnostics."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from filelock import FileLock, Timeout

from companion_core.errors import AssistantError
from companion_core.operation_context import Trace, bounded, remaining, stage

from .config import metadata, secrets_for, urls

START_SECONDS = 60
CONTROL_SECONDS = 40


def ready(config):
    try:
        response = httpx.get(urls(config)["admin"] + "/healthz", timeout=remaining(2), trust_env=False)
        return response.status_code == 200 and response.json().get("id") == config["id"]
    except (httpx.HTTPError, ValueError):
        return False


def startup_state(config, process):
    """Only safe enumerated fields from this launch; no raw logs or credentials."""
    try:
        path = Path(config["data"]) / "runtime" / "service-startup.json"
        if path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("pid") != process.pid:
            return {}
        steps = value.get("stages", [])
        allowed = {"database_start", "credential_store", "database_connect", "data_validation", "database_lock",
                   "database_status", "database_recovery", "service_open", "supervisor_start", "service_lifetime"}
        result = []
        for item in steps[-32:]:
            if item.get("stage") not in allowed or item.get("status") not in {"running", "completed", "failed"}:
                continue
            row = {"stage": item["stage"], "status": item["status"]}
            code = item.get("code")
            if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code):
                row["code"] = code
            result.append(row)
        return {"startup_stages": result}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def ensure_service(config):
    root = Path(config["data"]) / "runtime"
    process = None
    try:
        with stage("runtime_directory"):
            root.mkdir(parents=True, exist_ok=True)
        with bounded(START_SECONDS, "service_start_timeout"), stage("service_start_lock"):
            with FileLock(str(root / "service-launch.lock"), timeout=remaining(10)):
                with stage("service_health"):
                    if ready(config):
                        return
                with stage("service_spawn"):
                    # Persistent services use files, never the caller's captured pipes.
                    with (root / "service.log").open("ab") as log:
                        process = subprocess.Popen([sys.executable, "-m", "companion_service", "serve", "--project", config["project"]],
                            cwd=config["project"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                with stage("service_readiness"):
                    while True:
                        if ready(config):
                            return
                        if process.poll() is not None:
                            error = AssistantError("service_start_failed", "服务进程在就绪前退出，请查看启动阶段诊断")
                            error.startup = startup_state(config, process)
                            raise error
                        time.sleep(remaining(.2))
    except Timeout:
        raise AssistantError("service_start_busy", "其他请求正在启动服务，请稍后检查 doctor 状态") from None
    except PermissionError:
        raise AssistantError("execution_denied", "执行环境拒绝启动服务，请使用有权限的项目终端重试；不要修改聊天身份") from None
    except OSError:
        raise AssistantError("service_launch_failed", "无法创建服务进程，请检查项目运行环境与启动权限") from None
    except AssistantError as exc:
        if process is not None:
            exc.startup = startup_state(config, process)
            if process.poll() is not None:
                exc.startup["exit_code"] = process.poll()
        raise


def call(project, operation, arguments, *, thread=None):
    trace = Trace("plugin_control")
    try:
        with trace.activate(), bounded(105, "service_call_timeout"):
            value = _call(project, operation, arguments, thread=thread)
        return {**value, "diagnostics": trace.snapshot()}
    except AssistantError as exc:
        if hasattr(exc, "startup"):
            exc.diagnostics.update(exc.startup)
        if hasattr(exc, "server_diagnostics"):
            exc.diagnostics["server"] = exc.server_diagnostics
        raise


def _call(project, operation, arguments, *, thread=None):
    if operation == "service_setup":
        operation = "setup_ticket"
    if operation == "service_password_reset":
        operation = "password_reset_ticket"
    with stage("installation_metadata"):
        config = metadata(project)
        if config is None:
            raise AssistantError("service_missing", "尚未配置本机服务")
    with stage("host_identity_presence"):
        thread = thread or os.environ.get("CODEX_THREAD_ID")
        if not thread:
            raise AssistantError("host_identity_missing", "请从 Codex 当前聊天的 CLI 调用；共享 MCP 进程不提供可信聊天身份")
    ensure_service(config)
    with stage("credential_store"):
        secret = secrets_for(config)["bridge_secret"]
    with stage("control_request"):
        try:
            reply = httpx.post(urls(config)["admin"] + "/control", timeout=remaining(CONTROL_SECONDS), trust_env=False,
                headers={"Authorization": "Bearer " + secret},
                json={"thread": thread, "operation": operation, "arguments": arguments})
        except httpx.TimeoutException:
            raise AssistantError("service_control_timeout", "控制请求等待超时，结果尚未确认；先查询状态，勿直接重复提交") from None
        except httpx.HTTPError:
            raise AssistantError("service_control_unavailable", "控制连接中断，结果尚未确认；请检查服务状态") from None
        try:
            value = reply.json()
            if not isinstance(value, dict):
                raise ValueError()
        except ValueError:
            raise AssistantError("service_control_invalid", "控制入口返回了无法识别的响应，请查看服务阶段诊断") from None
        if reply.status_code != 200:
            error = AssistantError(value.get("error", "control_failed"), value.get("message", "控制操作失败"))
            # The authenticated local service returns sanitized stages only.
            if isinstance(value.get("diagnostics"), dict):
                error.server_diagnostics = value["diagnostics"]
            raise error
    if operation == "setup_ticket":
        return {"setup_url": urls(config)["admin"] + "/#setup=" + value["ticket"], "expires_in_seconds": 600}
    if operation == "password_reset_ticket":
        return {"reset_url": urls(config)["admin"] + "/#reset=" + value["ticket"], "expires_in_seconds": 600,
                "role_started": False}
    return {**value, "web": urls(config)}
