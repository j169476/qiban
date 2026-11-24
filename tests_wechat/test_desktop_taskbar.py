import asyncio
import ctypes
import os
import re
import threading
import time
from types import SimpleNamespace

import pytest

from qiban import desktop_wechat_window as windows
from qiban.desktop_scheduler import DesktopScheduler


class Desktop:
    def __init__(self):
        self.window = windows.Window(11, 22)
        self.requests = []

    def hidden_main(self):
        return self.window

    def minimize_hidden(self, expected):
        if expected != self.window:
            return False
        self.requests.append(expected)
        return True


def test_debounce_restart_and_retry_only_while_same_window_is_hidden():
    clock = [0]
    desktop = Desktop()
    policy = windows.WeChatTaskbar(desktop, lambda: clock[0])
    for t in (0, .1, .39):
        clock[0] = t
        assert policy.observe() is None
    clock[0] = .8
    old = policy.observe()
    # The client exited and Windows reused its HWND before restoration.
    new = windows.Window(old.hwnd, old.pid + 1)
    desktop.window = new
    policy.restore(old)
    assert desktop.requests == [] and policy.observe() is None
    for t in (1.3, 2, 5, 6.4, 20, 200):
        clock[0] = t
        if window := policy.observe():
            policy.restore(window)
    assert desktop.requests == [new, new]
    desktop.window = None  # either visible or normally minimized
    assert policy.observe() is None
    desktop.window = new
    assert policy.observe() is None
    clock[0] += 1
    assert policy.observe() == new


@pytest.mark.parametrize("status,generation,blocked,stopped,requests", [
    ("active", "first", False, False, 1),
    ("active", "next", False, False, 0),
    ("paused", "first", False, False, 0),
    ("active", "first", True, False, 0),
    ("active", "first", False, True, 0),
])
def test_scheduler_rechecks_committed_run_before_window_change(status, generation, blocked, stopped, requests):
    clock = [0]
    desktop = Desktop()
    policy = windows.WeChatTaskbar(desktop, lambda: clock[0])
    run = {"status": status, "generation": generation}
    runtime = SimpleNamespace(tasks=SimpleNamespace(blocked=blocked), require_current=lambda: None,
                              store=SimpleNamespace(lock=threading.RLock(), state=lambda key, default: run))
    scheduler = DesktopScheduler(runtime, wechat_taskbar=policy)
    if stopped:
        scheduler.stop_requested.set()
    scheduler._update_wechat_window("first")
    clock[0] = 1
    scheduler._update_wechat_window("first")
    assert len(desktop.requests) == requests


def test_window_worker_follows_platform_start_stop_and_shutdown():
    async def run_lifecycle():
        desktop = Desktop()
        run = {"status": "paused", "generation": "run-1"}
        store = SimpleNamespace(lock=threading.RLock(), state=lambda key, default: run,
                                read=lambda kind: {"data": {}})
        async def empty_inbox():
            return []
        adapter = SimpleNamespace(runtime_poll=empty_inbox)
        core = SimpleNamespace(run_key="core-run:wechat", store=store, guard=lambda *args: run,
                               platforms=SimpleNamespace(all=lambda: [adapter]))
        runtime = SimpleNamespace(tasks=SimpleNamespace(blocked=False), cores={"wechat": core},
                                  store=store, require_current=lambda: None)
        scheduler = DesktopScheduler(runtime, wechat_taskbar=windows.WeChatTaskbar(desktop))
        await scheduler._step("wechat")
        assert scheduler.wechat_window_generation is None
        run["status"] = "active"
        await scheduler._step("wechat")
        worker = asyncio.create_task(scheduler._watch_wechat_window())
        try:
            async with asyncio.timeout(4):
                while not desktop.requests:
                    await asyncio.sleep(.05)
            run["status"] = "paused"
            await scheduler._step("wechat")
            assert scheduler.wechat_window_generation is None
            await asyncio.sleep(.8)
            assert scheduler.wechat_taskbar.window is None
            assert len(desktop.requests) == 1
        finally:
            scheduler.stop_requested.set()
            await asyncio.wait_for(worker, 2)
    asyncio.run(run_lifecycle())


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop window manager")
def test_windows_minimizes_only_unique_hidden_main_without_activation(monkeypatch):
    # Exercise the real OS API on windows owned by THIS test process only.
    from ctypes import wintypes as w

    desktop = windows.WindowsDesktop()
    api = desktop.api
    api.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU,
                                    w.HINSTANCE, w.LPVOID]
    api.CreateWindowExW.restype = w.HWND
    api.DestroyWindow.argtypes = [w.HWND]
    api.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    api.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    initial = api.GetForegroundWindow()
    if not initial:
        pytest.skip("no interactive foreground on this Windows desktop")
    monkeypatch.setattr(windows, "_processes", lambda: [os.getpid()])
    monkeypatch.setattr(windows, "MAIN", "Static")
    handles = []

    def create_window(caption="Qiban taskbar test"):
        hwnd = api.CreateWindowExW(0, "Static", caption, 0x00CF0000,
                                   0, 0, 80, 60, None, None, None, None)
        assert hwnd
        handles.append(hwnd)
        return hwnd

    try:
        first = create_window()
        expected = windows.Window(first, os.getpid())
        assert desktop.hidden_main() == expected
        second = create_window()
        assert desktop.hidden_main() is None
        assert not desktop.minimize_hidden(expected)
        assert api.DestroyWindow(second)
        handles.remove(second)
        clock = [0]
        policy = windows.WeChatTaskbar(desktop, clock=lambda: clock[0])
        assert policy.observe() is None
        with monkeypatch.context() as patch:
            patch.setattr(api, "ShowWindowAsync", lambda hwnd, command: 0)
            for t in (1, 6):
                clock[0] = t
                assert policy.observe() == expected
                policy.restore(expected)
                clock[0] = t + 4
                assert policy.observe() is None
        clock[0] = 11
        assert policy.observe() == expected
        policy.restore(expected)
        deadline = time.monotonic() + 2
        msg = w.MSG()
        while time.monotonic() < deadline and not api.IsIconic(first):
            while api.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                api.DispatchMessageW(ctypes.byref(msg))
            time.sleep(.01)
        assert api.IsWindowVisible(first) and api.IsIconic(first)
        assert api.GetForegroundWindow() != first
        assert desktop.hidden_main() is None
        assert not desktop.minimize_hidden(expected)
        # Qt shares its window class between the main window and auxiliary windows.
        monkeypatch.setattr(windows, "MAIN", "unused")
        monkeypatch.setattr(windows, "QT_RENDER_WINDOW", re.compile("Static"))
        create_window()
        assert desktop.hidden_main() is None
        candidate = create_window("微信")
        assert desktop.hidden_main() == windows.Window(candidate, os.getpid())
    finally:
        for hwnd in handles:
            api.DestroyWindow(hwnd)
