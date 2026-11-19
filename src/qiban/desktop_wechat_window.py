"""Keep the user's WeChat desktop window on the taskbar without activating it."""
import ctypes
import time
from ctypes import wintypes as w
from dataclasses import dataclass

from companion_wechat.providers.uia import MAIN, QT_RENDER_WINDOW, _processes


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int


class WindowsDesktop:
    def __init__(self):
        self.api = api = ctypes.WinDLL("user32", use_last_error=True)
        self.enum_callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        for name, arguments, result in (
            ("EnumWindows", [self.enum_callback, w.LPARAM], w.BOOL),
            ("GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            ("GetWindow", [w.HWND, w.UINT], w.HWND),
            ("GetWindowLongW", [w.HWND, ctypes.c_int], w.LONG),
            ("GetClassNameW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetWindowTextW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("IsWindowVisible", [w.HWND], w.BOOL),
            ("IsIconic", [w.HWND], w.BOOL),
            ("GetForegroundWindow", [], w.HWND),
            ("ShowWindowAsync", [w.HWND, ctypes.c_int], w.BOOL),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result

    def _pid(self, hwnd):
        pid = w.DWORD()
        self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _main(self, hwnd):
        api = self.api
        if api.GetWindow(hwnd, 4) or api.GetWindowLongW(hwnd, -20) & 0x80:  # owner or tool window
            return False
        name = ctypes.create_unicode_buffer(128)
        api.GetClassNameW(hwnd, name, len(name))
        if name.value == MAIN:
            return True
        if not QT_RENDER_WINDOW.fullmatch(name.value) or not api.GetWindowLongW(hwnd, -16) & 0x20000:
            return False
        # Qt uses the same class for auxiliary windows. Match only the main app caption.
        api.GetWindowTextW(hwnd, name, len(name))
        return name.value.strip().casefold() in {"微信", "wechat", "weixin"}

    def hidden_main(self):
        pids = set(_processes())
        foreground = self.api.GetForegroundWindow()
        if not pids or not foreground or self._pid(foreground) in pids:
            return None
        matches = []

        @self.enum_callback
        def collect(hwnd, _):
            pid = self._pid(hwnd)
            if pid in pids and self._main(hwnd):
                matches.append(Window(hwnd, pid))
            return True

        if not self.api.EnumWindows(collect, 0) or len(matches) != 1:
            return None
        window = matches[0]
        if self.api.IsWindowVisible(window.hwnd) or self.api.IsIconic(window.hwnd):
            return None
        return window

    def minimize_hidden(self, expected):
        # Re-enumerate after debounce and check the foreground without activating any window.
        if self.hidden_main() != expected:
            return False
        if (self._pid(expected.hwnd) == expected.pid and not self.api.IsWindowVisible(expected.hwnd)
                and not self.api.IsIconic(expected.hwnd)):
            self.api.ShowWindowAsync(expected.hwnd, 7)  # SW_SHOWMINNOACTIVE
            return True
        return False


class WeChatTaskbar:
    def __init__(self, desktop, clock=time.monotonic):
        self.desktop = desktop
        self.clock = clock
        self.reset()

    def reset(self):
        self.window = None
        self.due = 0
        self.attempts = 0

    def observe(self):
        """Return a stable hidden window at most twice per continuous hide."""
        window = self.desktop.hidden_main()
        now = self.clock()
        if window != self.window:
            self.window = window
            self.due = now + 0.4
            self.attempts = 0
        if window is None or now < self.due or self.attempts >= 2:
            return None
        return window

    def restore(self, window):
        if self.desktop.minimize_hidden(window):
            self.attempts += 1
            self.due = self.clock() + 5
