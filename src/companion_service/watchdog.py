"""Independent, instance-bound process monitoring. Never starts/resumes a role."""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock

from companion_core.errors import AssistantError
from companion_core.storage import atomic_write, utcnow

STALE_SECONDS = 90


class ProcessHandle:
    """Keep the actual Windows process object, including its creation time."""
    def __init__(self, pid):
        from ctypes import wintypes

        if os.name != "nt":
            raise AssistantError("supervisor_platform", "独立进程监护目前需要 Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise AssistantError("supervisor_process", "无法核实服务进程")
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times)):
            self.close()
            raise AssistantError("supervisor_process", "无法核实服务进程创建时间")
        self.born = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def alive(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 0x102

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True).encode())


def pause_fault(store, code):
    """Reserve one existing management notification and pause atomically."""
    from .faults import record

    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        from companion_core.runtime_keys import RUN_KEYS

        changed = False
        for key in RUN_KEYS:
            run = store.state(key, {}, db)
            if run.get("status") != "active":
                continue
            changed = True
            record(SimpleNamespace(store=store), code, run_key=key)
            store.set_state(key, {**run, "status": "paused", "reason": code, "ended_at": utcnow()}, db)
            for table in ("core_actions", "actions"):
                db.execute("UPDATE " + table + " SET status='cancelled' WHERE generation=? AND status='prepared'",
                           (run["generation"],))
                db.execute("UPDATE " + table + " SET status='uncertain' WHERE generation=? AND status='dispatching'",
                           (run["generation"],))
        return changed


def inspect_parent(store, instance, *, alive, now=None):
    """Short cross-process lock protects the instance check and pause decision."""
    now = time.time() if now is None else now
    with FileLock(str(store.runtime / "service-process.lock"), timeout=5):
        parent = read(store.runtime / "service-process.json")
        if parent.get("instance") != instance or parent.get("closed"):
            return "finished"
        if alive and parent.get("maintenance_until", 0) > now:
            return "maintenance"
        stale = not isinstance(parent.get("at"), (int, float)) or now - parent["at"] > STALE_SECONDS
        if not alive or stale:
            pause_fault(store, "heartbeat_lost" if alive else "daemon_exited")
            return "stale" if alive else "exited"
        return "healthy"


class Supervisor:
    def __init__(self, service, project):
        self.service, self.store, self.project = service, service.store, Path(project).resolve()
        self.instance, self.process, self.closed = secrets.token_hex(16), None, False
        self.path = self.store.runtime / "service-process.json"
        parent = ProcessHandle(os.getpid())
        try:
            self.born = parent.born
        finally:
            parent.close()

    def pulse(self):
        with FileLock(str(self.store.runtime / "service-process.lock"), timeout=5):
            write(self.path, {"instance": self.instance, "pid": os.getpid(), "born": self.born,
                              "at": time.time(), "closed": self.closed,
                              "maintenance_until": self.service.maintenance_until})

    def start(self):
        self.pulse()
        self.process = subprocess.Popen([
            sys.executable, "-m", "companion_service", "watch", "--project", str(self.project),
            "--parent-pid", str(os.getpid()), "--parent-born", str(self.born), "--instance", self.instance],
            cwd=self.project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def status(self):
        heartbeat = read(self.store.runtime / "service-watchdog.json")
        healthy = (self.process is not None and self.process.poll() is None
                   and heartbeat.get("instance") == self.instance
                   and time.time() - heartbeat.get("at", 0) <= STALE_SECONDS)
        return {"healthy": healthy, "at": heartbeat.get("at") if heartbeat.get("instance") == self.instance else None,
                "separate_process": True}

    def ensure_ready(self, *, timeout=10):
        """Explicit recovery replaces only our child and waits for its own heartbeat."""
        if self.status()["healthy"]:
            return
        self.close()
        self.instance, self.process, self.closed = secrets.token_hex(16), None, False
        try:
            self.start()
            deadline = time.monotonic() + timeout
            while True:
                if self.status()["healthy"]:
                    return
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    break
                time.sleep(.1)
        except OSError:
            pass
        self.close()
        raise AssistantError("supervisor_lost", "独立监护未能恢复，平台保持暂停；请检查服务后重试")

    def close(self):
        self.closed = True
        self.pulse()
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()  # Only the exact child created by this instance.
                self.process.wait(timeout=5)


def watch(project, pid, born, instance):
    from companion_codex.host import CodexHost

    from .config import open_store
    from .faults import deliver

    try:
        parent = ProcessHandle(pid)
    except AssistantError:
        # The parent may exit before this child obtains a process handle. A
        # matching instance record still permits fail-closed pause, never a kill.
        parent = None
    store = host = None
    try:
        if parent is not None and parent.born != born:
            raise AssistantError("supervisor_process", "服务进程已更换，不处理其他进程")
        _, store, _ = open_store(project)
        recorded = read(store.runtime / "service-process.json")
        if (recorded.get("instance") != instance or recorded.get("pid") != pid or recorded.get("born") != born):
            raise AssistantError("supervisor_process", "服务实例记录已变化")
        host = CodexHost(Path(project))
        last_notice = 0
        while True:
            status = inspect_parent(store, instance, alive=parent.alive() if parent else False)
            if status == "finished":
                break
            write(store.runtime / "service-watchdog.json", {"instance": instance, "at": time.time(), "status": status})
            if status in {"stale", "exited"} and time.monotonic() - last_notice >= 30:
                last_notice = time.monotonic()
                deliver(SimpleNamespace(store=store, host=host))
            if status == "exited":
                break
            time.sleep(2)
    finally:
        if parent:
            parent.close()
        if host:
            host.close()
        if store:
            store.database.close()
