"""Official App Server client. No private database or rollout-file access."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from companion_core.errors import AssistantError

from . import __version__


class CodexHost:
    def __init__(self, project: Path, *, active_cache_seconds=0.0, desktop=None):
        if type(active_cache_seconds) not in (int, float) or not 0 <= active_cache_seconds <= 0.1:
            raise AssistantError("host_config", "宿主状态合并窗口无效")
        self.project = project.resolve()
        self.thread_id = os.environ.get("CODEX_THREAD_ID")
        self.process = None
        self.messages = queue.Queue()
        self.serial = 0
        self.lock = threading.RLock()
        self.active_cache_seconds = active_cache_seconds
        self._active_snapshot = None
        self.desktop = desktop

    def identity(self):
        if not self.thread_id:
            raise AssistantError("host_identity_missing", "没有宿主聊天 ID；请从 Codex 聊天内调用 CLI 入口")
        return self.thread_id

    def open_desktop(self):
        """An explicit admin click opens the configured workspace, never a turn."""
        executable = shutil.which("codex")
        if not executable:
            raise AssistantError("host_unavailable", "未找到 Codex CLI，请手动打开 Codex")
        try:
            result = subprocess.run(
                [executable, "app", str(self.project)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AssistantError("host_launch_failed", "未确认 Codex 已打开，请手动打开应用") from None
        if result.returncode:
            raise AssistantError("host_launch_failed", "Codex 未能打开，请手动打开应用")
        return {"launch_requested": True, "role_started": False}

    def connect(self):
        if self.process and self.process.poll() is None:
            return
        executable = shutil.which("codex")
        if not executable:
            raise AssistantError("host_unavailable", "未找到 Codex CLI")
        self.process = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        messages = queue.Queue()
        self.messages = messages
        proc = self.process

        def receive():
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass

        threading.Thread(target=receive, daemon=True).start()
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "qiban", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
        )
        proc.stdin.write('{"method":"initialized","params":{}}\n')
        proc.stdin.flush()

    def _request(self, method, params):
        self.serial += 1
        request_id = self.serial
        try:
            self.process.stdin.write(
                json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
            )
            self.process.stdin.flush()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                msg = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
                if msg.get("id") == request_id:
                    if "error" in msg:
                        raise AssistantError("host_protocol_error", f"{method} 调用失败")
                    return msg["result"]
        except (OSError, queue.Empty):
            self.close()
            raise AssistantError("host_unavailable", "无法核实宿主聊天状态，已阻止自主活动") from None
        raise AssistantError("host_timeout", "宿主状态查询超时")

    def request(self, method, params):
        with self.lock:
            self.connect()
            return self._request(method, params)

    def status(self, thread_id=None):
        tid = thread_id or self.identity()
        read = self.request("thread/read", {"threadId": tid, "includeTurns": False})
        thread = read["thread"]
        if Path(thread["cwd"]).resolve() != self.project:
            raise AssistantError("wrong_project", "聊天所属项目与数据空间不匹配")
        # Empty sourceKinds includes every source, including desktop/appServer/exec.
        # Both lists are paginated; absence from the first page is not evidence of archive.
        for archived in (True, False):
            cursor = None
            while True:
                params = {"cwd": str(self.project), "archived": archived, "sourceKinds": [], "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                page = self.request("thread/list", params)
                if any(item["id"] == tid for item in page["data"]):
                    return {"thread_id": tid, "archived": archived, "project": str(self.project)}
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        raise AssistantError("host_state_unknown", "宿主列表无法确认聊天状态")

    def require_active(self, thread_id=None):
        if self.desktop is not None:
            self.desktop.require_online()
        target = thread_id or self.identity()
        with self.lock:
            cached = self._active_snapshot
            if (cached is not None and self.active_cache_seconds > 0 and cached[0] == target
                    and time.monotonic() - cached[1] < self.active_cache_seconds
                    and self.process is not None and self.process.poll() is None):
                return dict(cached[2])
            # Failed/expired queries never fall back to an older active result.
            self._active_snapshot = None
            state = self.status(target)
            if state["archived"]:
                raise AssistantError("chat_archived", "归属角色聊天已归档")
            self._active_snapshot = (target, time.monotonic(), dict(state))
            return state

    def conversation_page(self, cursor=None):
        params = {"threadId": self.identity(), "limit": 20, "itemsView": "full", "sortDirection": "asc"}
        if cursor is not None:
            params["cursor"] = cursor
        return self.request("thread/turns/list", params)

    def notification_ready(self, thread_id):
        self.require_active(thread_id)
        thread = self.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
        state = thread.get("status", {})
        # Do not interrupt an ongoing management conversation.
        if state.get("type") not in ("idle", "notLoaded"):
            raise AssistantError("admin_busy", "管理聊天正在运行，故障提醒保留待发")

    def notify_admin(self, thread_id, message, notification_id):
        """One explicitly authorized notification turn, no permission/model overrides."""
        self.notification_ready(thread_id)
        self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        # A labelled tool result identifies the plugin as the source, not the human.
        result = self.request("turn/start", {
            "threadId": thread_id, "input": [],
            "toolOutput": {
                "name": "runtime_fault", "namespace": "bili_assistant",
                "output": message, "retain": True,
            },
            "turnTrigger": "bili_assistant_management_fault",
        })
        turn_id = result["turn"]["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            page = self.request("thread/turns/list", {
                "threadId": thread_id, "limit": 20, "itemsView": "full", "sortDirection": "desc",
            })
            for turn in page["data"]:
                if turn["id"] == turn_id and turn["status"] != "inProgress":
                    if turn["status"] != "completed":
                        raise AssistantError("notification_uncertain", "提醒回合未完成，请查看管理聊天")
                    return {"turn_id": turn_id, "notification_id": notification_id}
            time.sleep(2)
        raise AssistantError("notification_uncertain", "提醒回合等待超时，禁止自动重发")

    def close(self):
        self._active_snapshot = None
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
