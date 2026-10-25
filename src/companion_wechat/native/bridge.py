"""Bounded reusable native session; no process access until open()."""
from __future__ import annotations

import ctypes as c
import hashlib
import json
import os
import secrets
from ctypes import wintypes as w
from pathlib import Path

from companion_wechat.db_windows import _QT_CLASS

from .protocol import build_signature, fingerprint, pe_exports, require
from .windows import RemoteCalls, Windows


class Request(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ("size", "pid", "controller", "reserved")] + [
        (name, c.c_uint64) for name in ("born", "controller_born", "window", "deadline", "sequence")] + [
        ("session", c.c_char * 33)] + [(name, c.c_char * 128) for name in
                                      ("account", "owner", "family", "target")] + [("text", c.c_char * 1024)]


class Status(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in
               ("size", "stage", "error", "completed", "tasks", "completions", "callbacks", "active")] + [
        ("sequence", c.c_uint64)]


def validate_build(path):
    path = Path(path)
    data = path.read_bytes()
    manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    require(manifest == {"sources": build_signature(), "sha256": hashlib.sha256(data).hexdigest()},
            "bridge_build_changed")
    return pe_exports(data)


def window(windows, pid):
    """Resolve only the authenticated process's main-window class; never titles."""
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user = windows.u
    user.EnumWindows.argtypes, user.EnumWindows.restype = [callback_type, w.LPARAM], w.BOOL
    user.GetClassNameW.argtypes, user.GetClassNameW.restype = [w.HWND, w.LPWSTR, c.c_int], c.c_int
    user.IsWindowVisible.argtypes, user.IsWindowVisible.restype = [w.HWND], w.BOOL
    user.GetWindow.argtypes, user.GetWindow.restype = [w.HWND, w.UINT], w.HWND
    found = []

    @callback_type
    def collect(hwnd, _):
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, c.byref(owner))
        if owner.value == pid and user.IsWindowVisible(hwnd) and not user.GetWindow(hwnd, 4):
            name = c.create_unicode_buffer(256)
            if user.GetClassNameW(hwnd, name, len(name)) and (
                    name.value == "mmui::MainWindow" or _QT_CLASS.fullmatch(name.value)):
                # WeChat may have other Qt windows: only a minimized main window
                # is eligible. Ambiguity is refused, never resolved by chat title.
                if user.IsIconic(hwnd):
                    found.append(int(hwnd))
        return True

    require(user.EnumWindows(collect, 0) and len(found) == 1, "minimized_window_not_unique")
    return found[0]


class NativeBridge(RemoteCalls):
    def __init__(self, dll, account, targets, *, windows=None):
        self.dll, self.source, self.targets = Path(dll).resolve(), account, dict(targets)
        self.w = windows
        self.handle = self.base = None
        self.pending, self.attempted = False, False
        self.allocations = []
        self.sequence = 0
        self.request = None
        self.closed = False

    def check(self):
        require(not self.closed and self.handle and not self.pending, "native_session_closed")
        require(self.w.born(self.handle) == self.born, "process_changed")
        core = self.w.modules(self.pid).get("weixin.dll")
        require(core and self.w.account(self.handle, core[0]) == self.source, "source_mismatch")
        require(self.w.owner(self.hwnd) == self.pid and self.w.u.IsIconic(self.hwnd), "client_not_minimized")

    def _free(self, pointer):
        if self.pending:
            return  # The timed-out remote thread may still dereference it.
        for entry in list(self.allocations):
            address, size = entry
            if address == pointer:
                zeros, count = c.create_string_buffer(size), c.c_size_t()
                self.w.k.WriteProcessMemory(self.handle, address, zeros, size, c.byref(count))
                self.w.k.VirtualFreeEx(self.handle, address, 0, 0x8000)
                self.allocations.remove(entry)

    def _invoke(self, name, request, *, read=False, timeout=2000):
        pointer = self.allocate(bytes(request))
        try:
            code = self.call(self.base + self.exports[name], pointer, timeout=timeout)
            require(code == 0, "native_" + name.removeprefix("Xln").lower() + "_rejected")
            return type(request).from_buffer_copy(self.w.read(self.handle, pointer, c.sizeof(request))) if read else None
        finally:
            self._free(pointer)

    def open(self):
        require(self.handle is None and not self.closed, "native_session_state")
        self.exports = validate_build(self.dll)
        self.w = self.w or Windows()
        self.pid, self.born = self.w.select(self.source)
        self.hwnd = window(self.w, self.pid)
        modules = self.w.modules(self.pid)
        require(not ({"xln_native_probe.dll", "text_bridge.dll", "selftest_bridge.dll"} & set(modules)),
                "client_restart_required")
        fingerprint(modules["weixin.dll"][1])
        self.handle = self.w.api.open(self.pid, 0x10043A)
        self.check()
        loaded = modules.get(self.dll.name.lower())
        if loaded is None:
            pointer = self.allocate((str(self.dll) + "\0").encode("utf-16-le"))
            try:
                self.call(self.w.system_function(self.pid, b"LoadLibraryW"), pointer)
            finally:
                self._free(pointer)
            loaded = self.w.modules(self.pid).get(self.dll.name.lower())
        require(loaded and loaded[1].resolve() == self.dll, "bridge_load_failed")
        self.base = loaded[0]
        controller = self.w.api.open(os.getpid(), 0x1000)
        try:
            born = self.w.born(controller)
        finally:
            self.w.api.close(controller)
        self.request = Request(size=c.sizeof(Request), pid=self.pid, born=self.born, controller=os.getpid(),
                               controller_born=born, window=self.hwnd, session=secrets.token_hex(16).encode("ascii"),
                               account=self.source.encode("ascii"), owner=self.targets["owner_main"].encode("ascii"),
                               family=self.targets.get("family", "").encode("ascii"))
        self._invoke("XlnOpen", self.request, timeout=10000)
        return self

    @property
    def epoch(self):
        require(self.request is not None, "native_session_state")
        return hashlib.sha256(bytes(self.request.session) + str((self.pid, self.born)).encode()).hexdigest()

    def prepare(self, request):
        self.check()
        require(self.targets.get(request.conversation) == request.target_ref, "native_target_unbound")
        status = self.status()
        require(not status["error"] and not any(status[k] for k in ("tasks", "completions", "callbacks")),
                "native_previous_unsettled")
        self.sequence += 1
        self.request.sequence = self.sequence
        self.request.deadline = self.w.k.GetTickCount64() + 15000
        self.request.target = request.target_ref.encode("ascii")
        self.request.text = request.text.encode("utf-8")
        self._invoke("XlnPrepare", self.request)
        self.attempted = False

    def inspect(self):
        self.check()
        result = self.status()
        require(result["active"] and result["sequence"] == self.sequence and result["stage"] == 0
                and not result["error"] and self.w.k.GetTickCount64() <= self.request.deadline,
                "native_preparation_changed")

    def submit_once(self):
        require(not self.attempted, "native_already_attempted")
        self.check()
        self.attempted = True  # Never retry even if the thread returns no result.
        self._invoke("XlnCommit", self.request)

    def status(self):
        require(self.handle and self.base and not self.pending, "native_session_closed")
        value = self._invoke("XlnStatus", Status(), read=True)
        require(value.size == c.sizeof(Status), "native_status_invalid")
        return {name: getattr(value, name) for name, _ in Status._fields_ if name != "size"}

    def cancel(self):
        if self.handle and self.base and self.request is not None and not self.pending:
            self._invoke("XlnCancel", self.request)

    def close(self):
        self.closed = True
        try:
            if self.handle and self.base and self.request is not None and not self.pending:
                self._invoke("XlnClose", self.request)
        finally:
            if self.handle:
                for pointer, _size in list(self.allocations):
                    self._free(pointer)
                self.w.api.close(self.handle)
                self.handle = None
            if self.request is not None:
                c.memset(c.byref(self.request), 0, c.sizeof(self.request))
            # Never unload: native callbacks may still be returning into this DLL.
