# SPDX-License-Identifier: LicenseRef-helper-noncommercial-1.0
# Account object chain / system-export relocation adapted from Prometheus-first/helper
# a81c495770b397646ba0661118927c83589c41fc; see LICENSE.helper.
"""Explicit Windows adapter. Import is inert; only Bridge.load/submit write to WeChat."""
from __future__ import annotations

import ctypes as c
import struct
from ctypes import wintypes as w
from pathlib import Path

from companion_wechat.db_windows import READ_ACCESS, _ModuleEntry, _WindowsAPI

from .protocol import (
    PROFILES,
    ProbeFailure,
    fingerprint,
    personal,
    require,
)


class Windows:
    def __init__(self):
        self.api = _WindowsAPI()
        self.k, self.u = self.api.kernel, self.api.user
        for name, args, result in [
            ("GetProcessTimes", [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4, w.BOOL),
            ("GetTickCount64", [], c.c_ulonglong),
            ("VirtualAllocEx", [w.HANDLE, c.c_void_p, c.c_size_t, w.DWORD, w.DWORD], c.c_void_p),
            ("VirtualFreeEx", [w.HANDLE, c.c_void_p, c.c_size_t, w.DWORD], w.BOOL),
            ("WriteProcessMemory", [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], w.BOOL),
            ("CreateRemoteThread", [w.HANDLE, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)], w.HANDLE),
            ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            ("GetExitCodeThread", [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
            ("GetModuleHandleW", [w.LPCWSTR], w.HMODULE),
            ("GetProcAddress", [w.HMODULE, c.c_char_p], c.c_void_p),
            ("GetModuleHandleExW", [w.DWORD, c.c_void_p, c.POINTER(w.HMODULE)], w.BOOL),
            ("GetModuleFileNameW", [w.HMODULE, w.LPWSTR, w.DWORD], w.DWORD),
        ]:
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in [
            ("GetForegroundWindow", [], w.HWND), ("IsIconic", [w.HWND], w.BOOL),
            ("GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
        ]:
            fn = getattr(self.u, name)
            fn.argtypes, fn.restype = args, result

    def modules(self, pid):
        handle = self.api._snapshot(0x18, pid)
        entry = _ModuleEntry()
        entry.dwSize = c.sizeof(entry)
        result = {}
        try:
            ok = self.k.Module32FirstW(handle, c.byref(entry))
            while ok:
                result[entry.szModule.lower()] = (int(entry.modBaseAddr), Path(entry.szExePath))
                ok = self.k.Module32NextW(handle, c.byref(entry))
            return result
        finally:
            self.api.close(handle)

    def read(self, handle, address, length):
        require(0x10000 <= address < 0x800000000000 and 0 < length <= 4096, "invalid_address")
        value = self.api.read(handle, address, length)
        require(value is not None and len(value) == length, "read_failed")
        return bytes(value)

    def born(self, handle):
        values = [w.FILETIME() for _ in range(4)]
        require(self.k.GetProcessTimes(handle, *(c.byref(v) for v in values)), "process_changed")
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def owner(self, hwnd):
        pid = w.DWORD()
        require(self.u.GetWindowThreadProcessId(hwnd, c.byref(pid)), "window_missing")
        return pid.value

    def focus(self, pid, hwnd):
        foreground = int(self.u.GetForegroundWindow() or 0)
        require(foreground and self.owner(hwnd) == pid, "window_mismatch")
        return {"foreground": foreground, "minimized": bool(self.u.IsIconic(hwnd)),
                "target_foreground": self.owner(foreground) == pid}

    def account(self, handle, base):
        def read(address, size): return self.read(handle, address, size)
        def ptr(address): return struct.unpack("<Q", read(address, 8))[0]
        # Exact on-disk fingerprint is checked before this bounded object read.
        pe = struct.unpack("<I", read(base + 0x3C, 4))[0]
        require(64 <= pe < 1048576 and read(base + pe, 4) == b"PE\0\0", "unsupported_memory_core")
        matches = [p for p in PROFILES
                   if struct.unpack("<I", read(base + pe + 8, 4))[0] == p["timestamp"] and
                   struct.unpack("<I", read(base + pe + 80, 4))[0] == p["image_size"]]
        require(len(matches) == 1, "unsupported_memory_core")
        global_rva, app_table, app_getter, context_table, identity_getter = matches[0]["account_rvas"]
        app = ptr(base + global_rva)
        require(app != 0, "account_not_logged_in")
        require(ptr(app) == base + app_table and ptr(base + app_table + 0x60) == base + app_getter,
                "account_unavailable")
        context = ptr(app + 0x68)
        require(context != 0, "account_not_logged_in")
        require(ptr(context) == base + context_table and ptr(base + context_table + 0x18) == base + identity_getter and
                read(base + identity_getter, 5) == b"\x48\x8d\x41\x48\xc3", "account_unavailable")
        address = context + 0x48
        header = read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        require(1 <= length <= 127 and capacity >= length and
                (capacity == 15 or 16 <= capacity <= 1048576), "account_unavailable")
        buffer = address if capacity == 15 else struct.unpack_from("<Q", header)[0]
        body = read(buffer, length)
        value = body.decode("ascii")
        require(personal(value) and read(address, 32) == header and read(buffer, length) == body and
                ptr(base + global_rva) == app and ptr(app + 0x68) == context and
                ptr(app) == base + app_table and ptr(context) == base + context_table, "account_changed")
        return value

    def select(self, expected):
        matches = []
        for pid in self.api.process_ids():
            core = self.modules(pid).get("weixin.dll")
            if not core:
                continue
            profile = fingerprint(core[1])
            handle = self.api.open(pid, READ_ACCESS)
            try:
                require(self.api.identity(pid, handle).version == profile["version"], "unsupported_client")
                try:
                    account = self.account(handle, core[0])
                except ProbeFailure as exc:
                    if str(exc) == "account_not_logged_in":
                        continue
                    raise
                if account == expected:
                    matches.append((pid, self.born(handle)))
            finally:
                self.api.close(handle)
        require(len(matches) == 1, "source_not_unique")
        return matches[0]

    def system_function(self, pid, name):
        address = self.k.GetProcAddress(self.k.GetModuleHandleW("kernel32.dll"), name)
        owner = w.HMODULE()
        require(address and self.k.GetModuleHandleExW(6, address, c.byref(owner)), "system_export_missing")
        path = c.create_unicode_buffer(32768)
        require(self.k.GetModuleFileNameW(owner, path, len(path)), "system_export_missing")
        remote = self.modules(pid).get(Path(path.value).name.lower())
        require(remote is not None, "system_export_missing")
        return remote[0] + address - owner.value


class RemoteCalls:
    def allocate(self, data):
        ptr = self.w.k.VirtualAllocEx(self.handle, None, len(data), 0x3000, 0x04)
        require(ptr, "allocation_failed")
        self.allocations.append((ptr, len(data)))
        buffer, count = c.create_string_buffer(data), c.c_size_t()
        require(self.w.k.WriteProcessMemory(self.handle, ptr, buffer, len(data), c.byref(count)) and
                count.value == len(data), "write_failed")
        return ptr

    def call(self, address, parameter, timeout=10000):
        require(not self.pending, "previous_call_uncertain")
        thread = self.w.k.CreateRemoteThread(self.handle, None, 0, address, parameter, 0, None)
        require(thread, "call_not_created")
        self.pending = True
        try:
            require(self.w.k.WaitForSingleObject(thread, timeout) == 0, "call_uncertain")
            self.pending = False  # The thread ended, even if its return code cannot be read.
            code = w.DWORD()
            require(self.w.k.GetExitCodeThread(thread, c.byref(code)), "call_result_unavailable")
            return code.value
        finally:
            self.w.api.close(thread)

