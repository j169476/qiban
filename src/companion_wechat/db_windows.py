# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only Windows access for the opt-in database capability probe.

The cipher-object layout was studied in fanyuantaier/wechatauto-replica, commit
d322c4a9b1124228820bab363e5676016f188fc6, wechatauto/db.py (Apache-2.0).
This is a new restricted implementation: allowlisted versions/one PID, no key files,
no database discovery/decryption, no UI input, and bounded reads and lifetime.
No upstream module is imported or executed. See that project's LICENSE when
redistributing the adapted layout constants and extraction algorithm.

Mutable buffers owned here are cleared on release, on a best-effort basis.
Python and cryptographic libraries can make copies; this is not a promise that
every memory copy is securely erased or that the OS never pages process memory.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import struct
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

SUPPORTED_VERSION = "4.1.15.13"
SUPPORTED_VERSIONS = frozenset({SUPPORTED_VERSION, "4.1.15.56"})
READ_ACCESS = 0x0010 | 0x0400
_MAX_ADDRESS = 0x800000000000
_NAME = b"com.Tencent.WCDB.Config.Cipher"
_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64}(?:[0-9a-fA-F]{32})?)'")
_ALIAS = re.compile(r"[a-z][a-z0-9_]{0,63}")
_QT_CLASS = re.compile(r"Qt\d+QWindowIcon")
_ERRORS = {
    "unsupported_platform", "invalid_configuration", "client_not_running", "client_ambiguous",
    "client_version_unsupported", "client_identity_changed", "process_access_denied", "process_closed",
    "read_budget_exceeded", "scan_timeout", "scan_limit_exceeded", "process_query_failed",
    "invalid_memory_address", "key_not_found", "key_scan_failed", "invalid_database_page",
    "window_query_failed", "key_closed", "unknown_key_alias",
}


class WindowsProbeError(RuntimeError):
    """Fixed, redacted diagnostics only; never attach an OS error or a path."""

    def __init__(self, code):
        self.code = code if code in _ERRORS else "process_query_failed"
        super().__init__(self.code)


def _wipe(value):
    if isinstance(value, bytearray):
        value[:] = b"\0" * len(value)


def _address(address, size):
    return (type(address) is int and type(size) is int and 0x10000 <= address < _MAX_ADDRESS
            and 0 < size <= 1024 * 1024 and address + size <= _MAX_ADDRESS)


@dataclass(frozen=True)
class ClientInfo:
    pid: int = field(repr=False)
    executable: str = field(repr=False)
    version: str = ""


@dataclass(frozen=True)
class MemoryRegion:
    address: int
    size: int


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class _ModuleEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", wintypes.WCHAR * 256), ("szExePath", wintypes.WCHAR * 260)]


class _MemoryInfo(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]


class _WindowsAPI:
    """Native adapter. There are no mutation APIs in this interface."""

    def __init__(self):
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise WindowsProbeError("unsupported_platform")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        k = self.kernel
        for name, args, result in [
            ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("Process32FirstW", [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
            ("Process32NextW", [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
            ("Module32FirstW", [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)], wintypes.BOOL),
            ("Module32NextW", [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)], wintypes.BOOL),
            ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ("VirtualQueryEx", [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MemoryInfo),
                                ctypes.c_size_t], ctypes.c_size_t),
            ("ReadProcessMemory", [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        ]:
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, result

    def _snapshot(self, flags, pid):
        handle = self.kernel.CreateToolhelp32Snapshot(flags, pid)
        if handle == ctypes.c_void_p(-1).value:
            raise WindowsProbeError("process_query_failed")
        return handle

    def process_ids(self):
        handle = self._snapshot(2, 0)
        result = []
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = self.kernel.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() == "weixin.exe":
                    result.append(int(entry.th32ProcessID))
                ok = self.kernel.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.close(handle)
        return result

    def has_weixin_module(self, pid):
        handle = self._snapshot(0x08 | 0x10, pid)
        entry = _ModuleEntry()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = self.kernel.Module32FirstW(handle, ctypes.byref(entry))
            while ok:
                if entry.szModule.lower() == "weixin.dll":
                    return True
                ok = self.kernel.Module32NextW(handle, ctypes.byref(entry))
            return False
        finally:
            self.close(handle)

    def identity(self, pid, handle=None):
        owns = handle is None
        handle = self.open(pid, READ_ACCESS) if owns else handle
        try:
            size = wintypes.DWORD(32768)
            value = ctypes.create_unicode_buffer(size.value)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(size)):
                raise WindowsProbeError("process_query_failed")
            if PureWindowsPath(value.value).name.lower() != "weixin.exe":
                raise WindowsProbeError("client_identity_changed")
            return ClientInfo(pid, value.value, _file_version(value.value))
        finally:
            if owns:
                self.close(handle)

    def open(self, pid, access):
        handle = self.kernel.OpenProcess(access, False, pid)
        if not handle:
            raise WindowsProbeError("process_access_denied")
        return handle

    def close(self, handle):
        self.kernel.CloseHandle(handle)

    def read(self, handle, address, size):
        result = bytearray(size)
        buffer = (ctypes.c_ubyte * size).from_buffer(result)
        count = ctypes.c_size_t()
        ok = self.kernel.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count))
        if not ok or count.value != size:
            _wipe(result)
            return None
        return result

    def query_region(self, handle, address):
        mbi = _MemoryInfo()
        if not self.kernel.VirtualQueryEx(handle, address, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            return None
        # Only committed readable pages, never guards or execute-only pages.
        readable = (mbi.State == 0x1000 and not mbi.Protect & 0x100
                    and mbi.Protect & 0xFF in {0x02, 0x04, 0x08, 0x20, 0x40, 0x80})
        return int(mbi.BaseAddress or 0), int(mbi.RegionSize), bool(readable)

    def window_state(self, pid):
        user = self.user
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetClassNameW.restype = ctypes.c_int
        user.IsIconic.argtypes, user.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
        user.IsWindowVisible.argtypes, user.IsWindowVisible.restype = [wintypes.HWND], wintypes.BOOL
        user.GetWindow.argtypes, user.GetWindow.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
        user.GetForegroundWindow.argtypes, user.GetForegroundWindow.restype = [], wintypes.HWND
        states = []

        @callback_type
        def collect(hwnd, _):
            owner_pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value != pid or not user.IsWindowVisible(hwnd) or user.GetWindow(hwnd, 4):
                return True
            name = ctypes.create_unicode_buffer(256)
            if user.GetClassNameW(hwnd, name, len(name)):
                if name.value in {"mmui::MainWindow", "mmui::LoginWindow"} or _QT_CLASS.fullmatch(name.value):
                    states.append(bool(user.IsIconic(hwnd)))
            return True

        if not user.EnumWindows(collect, 0):
            raise WindowsProbeError("window_query_failed")
        foreground_pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(user.GetForegroundWindow(), ctypes.byref(foreground_pid))
        return {"window_count": len(states), "minimized_count": sum(states),
                "all_minimized": bool(states) and all(states), "foreground_is_target": foreground_pid.value == pid}


def _file_version(path):
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    unused = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(path, ctypes.byref(unused))
    if not 0 < size <= 1024 * 1024:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(path, 0, size, buffer):
        return ""
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if (not api.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length))
            or not pointer.value or length.value < 52):
        return ""
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if values[0] != 0xFEEF04BD:
        return ""
    hi, lo = values[2], values[3]
    return f"{hi >> 16}.{hi & 65535}.{lo >> 16}.{lo & 65535}"


def discover_wechat(*, pid=None, api=None):
    """Select a pinned PID or exactly one main client; never guess/fall back.

    A pinned PID is an explicit process selector, not proof of account identity.
    The caller must still authenticate the database account and fixed targets.
    Only the selected process's module/window/identity metadata is inspected.
    """
    if pid is not None and (type(pid) is not int or not 0 < pid < 2**32):
        raise WindowsProbeError("invalid_configuration")
    api = api or _WindowsAPI()
    candidates = []
    process_ids = api.process_ids()
    if pid is not None:
        if pid not in process_ids:
            raise WindowsProbeError("client_not_running")
        process_ids = [pid]
    for candidate_pid in process_ids:
        if api.has_weixin_module(candidate_pid) and api.window_state(candidate_pid)["window_count"]:
            candidates.append(api.identity(candidate_pid))
    if not candidates:
        raise WindowsProbeError("client_not_running")
    if len(candidates) != 1:
        raise WindowsProbeError("client_ambiguous")
    result = candidates[0]
    if pid is not None and result.pid != pid:
        raise WindowsProbeError("client_identity_changed")
    if result.version not in SUPPORTED_VERSIONS:
        raise WindowsProbeError("client_version_unsupported")
    return result


def window_state(pid, *, api=None):
    """Observe minimization/foreground state; never restore or activate a window."""
    if type(pid) is not int or not 0 < pid < 2**32:
        raise WindowsProbeError("invalid_configuration")
    return (api or _WindowsAPI()).window_state(pid)


class NativeProcess:
    """A single exact-version process with a fixed read budget and deadline."""

    def __init__(self, pid, *, max_total_bytes=4 * 1024**3, timeout=40.0, api=None, clock=time.monotonic):
        if (type(pid) is not int or not 0 < pid < 2**32 or type(max_total_bytes) is not int
                or not 4096 <= max_total_bytes <= 8 * 1024**3 or type(timeout) not in {int, float}
                or not 0 < timeout <= 60):
            raise WindowsProbeError("invalid_configuration")
        self._api, self._clock = api or _WindowsAPI(), clock
        self._deadline = clock() + timeout
        self._limit, self.bytes_read, self._handle = max_total_bytes, 0, None
        self._handle = self._api.open(pid, READ_ACCESS)
        try:
            info = self._api.identity(pid, self._handle)
            if info.version not in SUPPORTED_VERSIONS:
                raise WindowsProbeError("client_version_unsupported")
            self.version, self.pid = info.version, pid
        except BaseException as exc:
            self.close()
            if isinstance(exc, (WindowsProbeError, KeyboardInterrupt, SystemExit)):
                raise
            raise WindowsProbeError("process_query_failed") from None

    def __repr__(self):
        return "<NativeProcess read-only>"

    def _check(self):
        if self._handle is None:
            raise WindowsProbeError("process_closed")
        if self._clock() >= self._deadline:
            raise WindowsProbeError("scan_timeout")

    def read(self, address, size):
        self._check()
        if not _address(address, size):
            raise WindowsProbeError("invalid_memory_address")
        if self.bytes_read + size > self._limit:
            raise WindowsProbeError("read_budget_exceeded")
        self.bytes_read += size
        return self._api.read(self._handle, address, size)

    def regions(self):
        address = 0
        for _ in range(100000):
            self._check()
            region = self._api.query_region(self._handle, address)
            if region is None:
                return
            base, size, readable = region
            end = base + size
            if base < address or size <= 0 or end <= address or end > _MAX_ADDRESS:
                raise WindowsProbeError("process_query_failed")
            if readable and base >= 0x10000:
                yield MemoryRegion(base, size)
            address = end
            if address >= _MAX_ADDRESS:
                return
        raise WindowsProbeError("scan_limit_exceeded")

    def close(self):
        if self._handle is not None:
            self._api.close(self._handle)
            self._handle = None

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_):
        self.close()


class MemoryKeys:
    """Owns mutable in-process key buffers; never serializes them."""

    def __init__(self):
        self._keys = {}
        self._closed = False

    def __repr__(self):
        return "<MemoryKeys redacted>"

    def aliases(self):
        if self._closed:
            raise WindowsProbeError("key_closed")
        return tuple(self._keys)

    def key(self, alias):
        if self._closed:
            raise WindowsProbeError("key_closed")
        if alias not in self._keys:
            raise WindowsProbeError("unknown_key_alias")
        return memoryview(self._keys[alias]).toreadonly()

    def close(self):
        for key in self._keys.values():
            _wipe(key)
        self._keys.clear()
        self._closed = True

    def __enter__(self):
        if self._closed:
            raise WindowsProbeError("key_closed")
        return self

    def __exit__(self, *_):
        self.close()

    def __getstate__(self):
        raise TypeError("memory_keys_not_serializable")


def _scan(process, needles, *, max_hits=256, chunk_size=1024 * 1024):
    """Two bounded passes use mutable chunks, never a whole-process image."""
    hits = []
    seen = set()
    overlap = max(map(len, needles)) - 1
    for region in process.regions():
        tail = bytearray()
        try:
            for offset in range(0, region.size, chunk_size):
                chunk = process.read(region.address + offset, min(chunk_size, region.size - offset))
                if chunk is None:
                    _wipe(tail)
                    tail = bytearray()
                    continue
                if not isinstance(chunk, bytearray):
                    chunk = bytearray(chunk)
                combined = tail + chunk
                start = region.address + offset - len(tail)
                _wipe(tail)
                try:
                    for needle_index, needle in enumerate(needles):
                        cursor = 0
                        while (found := combined.find(needle, cursor)) >= 0:
                            hit = (needle_index, start + found)
                            if hit not in seen:
                                hits.append(hit)
                                seen.add(hit)
                                if len(hits) > max_hits:
                                    raise WindowsProbeError("scan_limit_exceeded")
                            cursor = found + 1
                    tail = combined[-overlap:] if overlap else bytearray()
                finally:
                    _wipe(chunk)
                    _wipe(combined)
        finally:
            _wipe(tail)
    return hits


def extract_keys(process, page1_by_alias, verify_key, *, require_all=True):
    """Return only keys that authenticate caller-selected encrypted page ones.

    ``verify_key(raw_key, page1)`` must authenticate the SQLCipher page, and must
    not persist/log its arguments. This function never opens a database file.
    By default a missing alias fails the whole extraction. ``require_all=False``
    returns partial/empty results, for fail-closed account-candidate selection.
    """
    if (getattr(process, "version", None) not in SUPPORTED_VERSIONS or not callable(verify_key)
            or type(require_all) is not bool
            or not isinstance(page1_by_alias, dict) or not 1 <= len(page1_by_alias) <= 16
            or any(not isinstance(k, str) or not _ALIAS.fullmatch(k) for k in page1_by_alias)):
        raise WindowsProbeError("invalid_configuration")
    if any(not isinstance(page, (bytes, bytearray)) or len(page) != 4096
           or page.startswith(b"SQLite format 3\0") for page in page1_by_alias.values()):
        raise WindowsProbeError("invalid_database_page")
    result = MemoryKeys()
    try:
        names = [address for _, address in _scan(process, [_NAME], max_hits=64)]
        if not names:
            if require_all:
                raise WindowsProbeError("key_not_found")
            return result
        refs = _scan(process, [struct.pack("<QQ", address, len(_NAME)) for address in names])
        tested = set()
        for name_index, reference in refs:
            if reference < 0x10010:
                continue
            node = process.read(reference - 0x10, 0x50)
            obj = blob = decoded = None
            try:
                if node is None or len(node) != 0x50:
                    continue
                if struct.unpack_from("<QQ", node, 0x10) != (names[name_index], len(_NAME)):
                    continue
                config = struct.unpack_from("<Q", node, 0x28)[0]
                if not _address(config + 0x88, 0x28):
                    continue
                obj = process.read(config + 0x88, 0x28)
                if obj is None or len(obj) != 0x28:
                    continue
                pointer, length = struct.unpack_from("<QQ", obj, 0x8)
                if not 0 < length <= 1024 or not _address(pointer, length):
                    continue
                blob = process.read(pointer, length)
                if blob is None or len(blob) != length:
                    continue
                decoded = bytearray(value ^ _MASK[i % len(_MASK)] for i, value in enumerate(blob))
                for match in _LITERAL.finditer(decoded):
                    key = bytearray.fromhex(match.group(1)[:64].decode("ascii"))
                    try:
                        digest = hashlib.sha256(key).digest()
                        if digest in tested:
                            continue
                        tested.add(digest)
                        if len(tested) > 256:
                            raise WindowsProbeError("scan_limit_exceeded")
                        for alias, page in page1_by_alias.items():
                            if alias not in result._keys and verify_key(key, page) is True:
                                result._keys[alias] = bytearray(key)
                        if len(result._keys) == len(page1_by_alias):
                            return result
                    finally:
                        _wipe(key)
            finally:
                for item in (node, obj, blob, decoded):
                    _wipe(item)
        if require_all:
            raise WindowsProbeError("key_not_found")
        return result
    except BaseException as exc:
        result.close()
        if isinstance(exc, (WindowsProbeError, KeyboardInterrupt, SystemExit)):
            raise
        raise WindowsProbeError("key_scan_failed") from None
