"""One background layout worker per desktop instance. Never joined by a reply."""
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone


class LearningIndex:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.running = None
        self.worker = None
        self.generation = 0
        self.closed = False
        self.local = threading.local()
        self.error = None
        self.retry_after = {}
        self.waiting = OrderedDict()
        self.last_success = None
        self.paused = store.state("desktop-tree-index-paused", False)

    def status(self):
        return {"paused": self.paused, "pending": len(self.waiting) + int(self.running is not None),
                "last_success": self.last_success, "error": self.error}

    def invalidate(self, *, closing=False):
        with self.lock:
            self.generation += 1
            self.closed |= closing
            self.waiting.clear()
            self.cache.clear()
            self.retry_after.clear()
            worker = self.worker
        if closing and worker:
            worker.join(timeout=5)

    def cancelled(self):
        return self.closed or self.local.generation != self.generation

    def control(self, paused):
        with self.lock:
            self.store.set_state("desktop-tree-index-paused", paused)
            self.paused = paused
            if not paused:
                self._start_next()
            return self.status()

    def get(self, key, build):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            if self.running != key and time.monotonic() >= self.retry_after.get(key, 0):
                # A new revision replaces any queued revision for the same view.
                self.waiting[key[0]] = (key, build)
                while len(self.waiting) > 4:
                    self.waiting.popitem(last=False)
                self._start_next()
            return None

    def _start_next(self):
        if self.closed or self.paused or self.running is not None or not self.waiting:
            return
        _, (key, build) = self.waiting.popitem(last=False)
        self.running = key
        self.worker = threading.Thread(target=self._build, args=(key, build, self.generation),
                                       name="qiban-learning-layout", daemon=True)
        self.worker.start()

    def previous(self, key, version):
        with self.lock:
            for cached_key, value in reversed(self.cache.items()):
                if cached_key[:-1] == key[:-1] and value[1] == version:
                    return value
            return None

    def latest(self, key):
        with self.lock:
            for previous, value in reversed(self.cache.items()):
                if previous[:-1] == key[:-1]:
                    return previous[-1], value[0]
            return None

    def _build(self, key, build, generation):
        self.local.generation = generation
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.GetCurrentThread.restype = wintypes.HANDLE
                api.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
                api.SetThreadPriority(api.GetCurrentThread(), -1)
            value = build()
            with self.lock:
                if not self.paused and not self.cancelled():
                    self.cache[key] = value
                    while len(self.cache) > 4:
                        self.cache.popitem(last=False)
                    self.last_success = datetime.now(timezone.utc).isoformat()
                    self.error = None
        except Exception as exc:
            with self.lock:
                if self.cancelled():
                    return
                self.error = getattr(exc, "code", "index_unavailable")
                if self.error == "index_paused":
                    self.waiting.setdefault(key[0], (key, build))
                self.retry_after = {k: t for k, t in self.retry_after.items() if t > time.monotonic()}
                self.retry_after[key] = time.monotonic() + 1
        finally:
            with self.lock:
                self.running = None
                self._start_next()
