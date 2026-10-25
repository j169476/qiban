"""Opt-in synthetic benchmark metrics; no SQL parameters or record text retained."""
import ctypes
import os
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from functools import lru_cache

import psycopg


class WindowsMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD),
                *[(key, ctypes.c_size_t) for key in (
                    "peak_working_set", "working_set", "peak_paged_pool", "paged_pool",
                    "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile")]]


@lru_cache(maxsize=1)
def _windows_memory_reader():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(WindowsMemoryCounters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    return kernel.GetCurrentProcess, psapi.GetProcessMemoryInfo


def rss():
    if os.name == "nt":
        # ctypes retains pointer types globally; redefining this structure on
        # every 25 ms sample makes the observer itself grow throughout the run.
        value = WindowsMemoryCounters()
        value.cb = ctypes.sizeof(value)
        current_process, get_memory = _windows_memory_reader()
        if not get_memory(current_process(), ctypes.byref(value), value.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"rss_bytes": value.working_set, "process_peak_rss_bytes": value.peak_working_set}
    from pathlib import Path

    values = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    return {"rss_bytes": int(values["VmRSS"].split()[0]) * 1024,
            "process_peak_rss_bytes": int(values["VmHWM"].split()[0]) * 1024}


class Metrics:
    """Count actual client SQL executions, excluding implicit driver transactions.

    COPY counts as one operation; executemany counts each parameter row. BEGIN /
    COMMIT / savepoints emitted internally by psycopg are not included. Counters
    observe only this benchmark process, not the whole PostgreSQL server.
    """
    def __init__(self):
        self.statements = 0
        self._saved = {}

    def __enter__(self):
        for name in ("execute", "executemany", "copy"):
            original = getattr(psycopg.Cursor, name)
            self._saved[name] = original

            def wrapped(cursor, query, *args, _name=name, _original=original, **kwargs):
                if _name == "executemany":
                    rows = list(args[0])
                    args = (rows, *args[1:])
                    self.statements += len(rows)
                else:
                    self.statements += 1
                return _original(cursor, query, *args, **kwargs)

            setattr(psycopg.Cursor, name, wrapped)
        return self

    def __exit__(self, *args):
        for name, original in self._saved.items():
            setattr(psycopg.Cursor, name, original)

    @contextmanager
    def stage(self, report, key):
        started, before = time.perf_counter(), self.statements
        measured = rss()
        samples = [measured["rss_bytes"]]
        stop = threading.Event()

        def sample():
            while not stop.wait(.025):
                samples.append(rss()["rss_bytes"])

        worker = threading.Thread(target=sample, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join()
            latest = rss()
            report[key] = {"seconds": round(time.perf_counter() - started, 4),
                           "client_sql_statements": self.statements - before,
                           "rss_before_bytes": measured["rss_bytes"], **latest,
                           "sampled_peak_rss_bytes": max(*samples, latest["rss_bytes"]),
                           "rss_sample_interval_ms": 25}
