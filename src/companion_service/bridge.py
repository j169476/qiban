"""Trusted CLI bridge. No role context or vault material is printed to Codex."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from filelock import FileLock

from companion_core.errors import AssistantError

from .config import metadata, secrets_for, urls


def ready(config):
    try:
        response = httpx.get(urls(config)["admin"] + "/healthz", timeout=2, trust_env=False)
        return response.status_code == 200 and response.json().get("id") == config["id"]
    except (httpx.HTTPError, ValueError):
        return False


def ensure_service(config):
    root = Path(config["data"]) / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "service-launch.lock"), timeout=25):
        if ready(config):
            return
        # A background web service starts paused; this does not start a role.
        with (root / "service.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "companion_service", "serve", "--project", config["project"]],
                cwd=config["project"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(80):
            if ready(config):
                return
            if process.poll() is not None:
                break
            time.sleep(.2)
        raise AssistantError("service_unavailable", "本机服务未能就绪，角色保持暂停")


def call(project, operation, arguments, *, thread=None):
    if operation == "service_setup":
        operation = "setup_ticket"
    if operation == "service_password_reset":
        operation = "password_reset_ticket"
    config = metadata(project)
    if config is None:
        raise AssistantError("service_missing", "尚未配置本机服务")
    thread = thread or os.environ.get("CODEX_THREAD_ID")
    if not thread:
        raise AssistantError("host_identity_missing", "请从 Codex 当前聊天的 CLI 调用；共享 MCP 进程不提供可信聊天身份")
    ensure_service(config)
    secret = secrets_for(config)["bridge_secret"]
    try:
        reply = httpx.post(urls(config)["admin"] + "/control", timeout=40, trust_env=False,
                           headers={"Authorization": "Bearer " + secret},
                           json={"thread": thread, "operation": operation, "arguments": arguments})
        value = reply.json()
    except (httpx.HTTPError, ValueError):
        raise AssistantError("service_unavailable", "本机控制入口不可用，未执行替代路径") from None
    if reply.status_code != 200:
        raise AssistantError(value.get("error", "control_failed"), value.get("message", "控制操作失败"))
    if operation == "setup_ticket":
        return {"setup_url": urls(config)["admin"] + "/#setup=" + value["ticket"], "expires_in_seconds": 600}
    if operation == "password_reset_ticket":
        return {"reset_url": urls(config)["admin"] + "/#reset=" + value["ticket"], "expires_in_seconds": 600,
                "role_started": False}
    return {**value, "web": urls(config)}
