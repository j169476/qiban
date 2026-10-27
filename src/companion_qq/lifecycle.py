"""Own only the process group created for this explicit QQ run.

Windows Job handles (including descendant membership) are the ownership proof.
Persisted PIDs are diagnostic metadata, never authority to terminate a process.
"""
import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from companion_core.errors import AssistantError
from companion_core.process_scope import ProcessScope


class ManagedBridge:
    def __init__(self, store, options, *, process_factory=subprocess.Popen, scope_factory=ProcessScope):
        self.store, self.options = store, options
        self.process_factory, self.scope_factory = process_factory, scope_factory
        self.process = self.scope = None
        self.state, self.code = "new", ""
        self.key = "qq-managed-bridge:" + options["account"]

    def port_open(self):
        endpoint = urlsplit(self.options["http"])
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=.3):
                return True
        except OSError:
            return False

    def launch(self):
        launch = self.options.get("launch")
        if not launch:
            raise AssistantError("qq_launch_not_configured", "请在平台接入中设置 NapCat 启动程序，或先手动启动连接器")
        if not Path(launch["executable"]).is_file() or not Path(launch["directory"]).is_dir():
            raise AssistantError("qq_launch_configuration", "NapCat 启动程序或工作目录不存在")
        if os.name == "nt" and Path(launch["executable"]).suffix.lower() != ".exe":
            raise AssistantError("qq_launch_configuration", "请选择可执行程序；脚本需通过明确指定的解释器启动")
        self.state = "launching"
        try:
            self.scope = self.scope_factory()
            self.process = self.process_factory([launch["executable"], *launch["arguments"]],
                cwd=launch["directory"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | (4 if os.name == "nt" else 0), start_new_session=os.name != "nt")
            self.scope.attach(self.process)
            self.store.set_state(self.key, {"pid": self.process.pid, "created_at": time.time(),
                "executable": launch["executable"], "directory": launch["directory"],
                "ownership": "live_process_scope", "status": "starting"})
            self.scope.resume()
        except Exception:
            self.stop()
            raise AssistantError("qq_launch_failed", "NapCat 启动失败，请检查启动程序与参数") from None

    async def ensure(self, provider, guard):
        try:
            await asyncio.to_thread(guard)
            try:
                identity = await asyncio.to_thread(provider.probe)
                self.state = "connecting"
                return identity  # Healthy external instances are never adopted or killed.
            except AssistantError as exc:
                if exc.code not in {"qq_provider_unavailable", "qq_login_required"}:
                    raise
                waiting_login = exc.code == "qq_login_required"
            if not waiting_login and self.scope is None:
                if await asyncio.to_thread(self.port_open):
                    raise AssistantError("qq_port_conflict", "NapCat 端口已有响应但身份核验失败，请检查端口与 Token")
                await asyncio.to_thread(self.launch)
            end = time.monotonic() + self.options.get("launch", {}).get("timeout", 90)
            while time.monotonic() < end:
                await asyncio.to_thread(guard)
                self.state = "waiting_login" if waiting_login else "launching"
                try:
                    identity = await asyncio.to_thread(provider.probe)
                    self.state = "connecting"
                    return identity
                except AssistantError as exc:
                    if exc.code not in {"qq_provider_unavailable", "qq_login_required"}:
                        raise
                    waiting_login = exc.code == "qq_login_required"
                await asyncio.sleep(.5)
            raise AssistantError("qq_login_required" if waiting_login else "qq_launch_timeout",
                                 "NapCat 尚未就绪，请完成连接器登录或检查启动配置")
        except BaseException as exc:
            self.state, self.code = "failed", getattr(exc, "code", "qq_launch_failed")
            await asyncio.to_thread(self.stop)
            raise

    def stop(self):
        scope, self.scope = self.scope, None
        if scope is not None:
            try:
                scope.close()  # Waits for owned processes and descendants, not arbitrary PIDs.
            except Exception:
                self.code = "qq_shutdown_failed"
                raise AssistantError(self.code, "NapCat 托管进程退出未能确认，请检查连接器状态") from None
            finally:
                self.process = None
                self.store.set_state(self.key, {"status": "stopped", "ownership": "released",
                                               "port_still_open": self.port_open()})
        if self.state != "failed":
            self.state = "paused"
