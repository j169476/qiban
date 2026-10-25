"""Observe the local desktop process, never kill or relaunch it.

The CLI executable is not proof that the desktop is online. Pin a real process
handle so PID reuse and restarting the desktop cannot silently resume a run.
"""
import ctypes
import os
import threading
from ctypes import wintypes

from companion_core.errors import AssistantError


class ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                ("exe", wintypes.WCHAR * 260)]


class Desktop:
    def __init__(self):
        self.lock = threading.RLock()
        self.handle, self.pid = None, None
        self.api = None
        if os.name == "nt":
            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = {
                "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
                "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL),
                "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL),
                "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
                "ProcessIdToSessionId": ([wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
                "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
                "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            }
            for name, (args, result) in signatures.items():
                getattr(self.api, name).argtypes = args
                getattr(self.api, name).restype = result
            self.adopt()

    def candidates(self):
        if not self.api:
            return []
        current = wintypes.DWORD()
        if not self.api.ProcessIdToSessionId(os.getpid(), ctypes.byref(current)):
            return []
        snapshot = self.api.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            return []
        found = []
        try:
            entry = ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            more = self.api.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.exe.lower() in ("chatgpt.exe", "codex.exe"):
                    session = wintypes.DWORD()
                    if (self.api.ProcessIdToSessionId(entry.pid, ctypes.byref(session))
                            and session.value == current.value):
                        handle = self.api.OpenProcess(0x101000, False, entry.pid)
                        if handle:
                            name, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                            ok = self.api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length))
                            image = name.value.replace("\\", "/").lower()
                            # Windows Store desktop or the unpackaged desktop layout;
                            # the CLI's .../Codex/bin/.../codex.exe is deliberately excluded.
                            desktop = ("/windowsapps/openai.codex_" in image and "/app/" in image
                                       or image.endswith("/codex/chatgpt.exe") or image.endswith("/codex/codex.exe"))
                            if ok and desktop:
                                found.append((entry.pid, entry.parent, handle))
                            else:
                                self.api.CloseHandle(handle)
                more = self.api.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        return found

    def adopt(self):
        with self.lock:
            candidates = self.candidates()
            pids = {pid for pid, _, _ in candidates}
            roots = [item for item in candidates if item[1] not in pids]
            selected = roots[0] if len(roots) == 1 else None
            for item in candidates:
                if item != selected:
                    self.api.CloseHandle(item[2])
            self.close()
            if selected:
                self.pid, _, self.handle = selected
            return self.status()

    def status(self):
        with self.lock:
            online = bool(self.handle and self.api.WaitForSingleObject(self.handle, 0) == 0x102)
            return {"available": online, "status": "online" if online else "stopped" if self.pid else "unavailable"}

    def require_online(self):
        if not self.status()["available"]:
            raise AssistantError("host_stopped", "ChatGPT已停止运行；请在管理聊天或命令行重启服务")

    def close(self):
        with self.lock:
            if self.handle:
                self.api.CloseHandle(self.handle)
                self.handle = None
