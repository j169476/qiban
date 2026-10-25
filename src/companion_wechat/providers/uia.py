"""Optional Windows UIA implementation of the P0 read-only probe.

Only process metadata, window classes, generic list labels and control structure
are inspected. This module has no input, focus, navigation or sending methods.
Selectors were checked against pywechat's public 4.1 UI definitions; they remain
capability probes rather than promises about a particular client/account.
"""
from __future__ import annotations

import ctypes
import re
from collections import deque
from ctypes import wintypes
from importlib.metadata import PackageNotFoundError, version
from pathlib import PureWindowsPath

from ..probe import DesktopSnapshot, ProbeUnavailable, Structure, TargetSnapshot

MAIN = "mmui::MainWindow"
LOGIN = "mmui::LoginWindow"
CHAT = "mmui::ChatSingleWindow"
MESSAGE_LISTS = {"消息", "Messages", "訊息"}
SESSION_LISTS = {"会话", "Chats", "對話"}
INPUT = "mmui::ChatInputField"
QT_RENDER_WINDOW = re.compile(r"Qt\d+QWindowIcon")
RENDER_PANE = "MMUIRenderSubWindowHW"


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _processes():
    """Toolhelp process names only; do not inspect command lines or memory maps."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ProbeUnavailable("probe_failed")
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    result = []
    try:
        has_entry = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while has_entry:
            if entry.szExeFile.lower() in {"weixin.exe", "wechat.exe"}:
                result.append(entry.th32ProcessID)
            has_entry = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return result


def _file_version(pid):
    """Read the executable's version resource, never a profile or data directory."""
    import win32api

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        path = buffer.value
        if PureWindowsPath(path).name.lower() not in {"weixin.exe", "wechat.exe"}:
            return ""
        values = win32api.GetFileVersionInfo(path, "\\")
        high, low = values["FileVersionMS"], values["FileVersionLS"]
        return f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"
    except Exception:
        return ""
    finally:
        kernel.CloseHandle(handle)


def inspect_structure(window, *, target=False, max_nodes=256, max_depth=8):
    """Bounded structural traversal; skip every list's content by default.

    Even for explicitly selected targets, message items are only counted. No
    message node's Name, text, Value, sender, runtime ID or contents are read.
    """
    pending = deque([(window, 0)])
    nodes = lists = items = inputs = 0
    sessions = truncated = False
    while pending and nodes < max_nodes:
        node, depth = pending.popleft()
        nodes += 1
        control_type = node.element_info.control_type
        if node.class_name() == INPUT:
            inputs += 1
        if control_type == "List":
            label = node.window_text()
            sessions |= label in SESSION_LISTS
            if label in MESSAGE_LISTS:
                lists += 1
                if target:
                    # Do not enumerate grandchildren or retrieve item text.
                    children = node.children(control_type="ListItem")
                    items += min(len(children), max_nodes)
                    truncated |= len(children) > max_nodes
            continue
        if depth >= max_depth:
            truncated = True
            continue
        children = node.children()
        remaining = max_nodes - nodes - len(pending)
        if len(children) > remaining:
            truncated = True
        pending.extend((child, depth + 1) for child in children[:max(0, remaining)])
    truncated |= bool(pending)
    return Structure(nodes, truncated, sessions, lists, items, inputs)


def _combined(structures):
    return Structure(sum(s.nodes for s in structures), any(s.truncated for s in structures),
                     any(s.session_list for s in structures), sum(s.message_lists for s in structures),
                     0, sum(s.input_fields for s in structures))


class WindowsUIABackend:
    """Constructed only on Windows and only after a probe was explicitly run."""

    def snapshot(self, targets):
        try:
            dependency_version = version("pywinauto")
            from pywinauto import Desktop
        except (ImportError, PackageNotFoundError):
            raise ProbeUnavailable("dependency_missing") from None
        except Exception:
            raise ProbeUnavailable("dependency_unavailable") from None
        processes = _processes()
        versions = tuple(v for pid in processes if (v := _file_version(pid)))
        desktop = Desktop(backend="uia")
        windows = []
        for pid in processes:
            # Do not filter classes during discovery: a client update must not
            # be mistaken for there being no window. Unrecognized windows are
            # counted; the specifically observed Qt renderer gets a bounded
            # structural check without looking at window titles.
            windows.extend(desktop.windows(process=pid, visible_only=True))
        mains = [w for w in windows if w.class_name() == MAIN]
        logins = [w for w in windows if w.class_name() == LOGIN]
        chats = [w for w in windows if w.class_name() == CHAT]
        unknown = len(windows) - len(mains) - len(logins) - len(chats)
        renderers = []
        for window in windows:
            if (QT_RENDER_WINDOW.fullmatch(window.class_name())
                    and window.element_info.control_type == "Window"):
                # This surface has been observed in the actual 4.1 client. Its
                # presence does not establish login, account or chat identity.
                children = window.children()
                if any(node.class_name() == RENDER_PANE and node.element_info.control_type == "Pane"
                       for node in children[:256]):
                    renderers.append(inspect_structure(window))
        # Multiple main windows are not assumed to belong to one account.
        structures = [inspect_structure(w) for w in mains]
        main_structure = _combined(structures)
        observed = []
        for target in targets:
            try:
                matches = [w for w in chats if w.window_text() == target.title]
                structure = inspect_structure(matches[0], target=True) if len(matches) == 1 else Structure()
                observed.append(TargetSnapshot(target.alias, len(matches), structure))
            except Exception:
                observed.append(TargetSnapshot(target.alias, failed=True))
        return DesktopSnapshot(len(processes), tuple(v for v in versions if re.fullmatch(r"\d+(\.\d+){3}", v)),
                               len(mains), len(logins), len(chats), main_structure, tuple(observed),
                               dependency_version, unknown, len(renderers), _combined(renderers))
