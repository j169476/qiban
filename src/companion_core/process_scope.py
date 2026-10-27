"""Keep a worker or connector and its descendants inside an owned process scope."""
import ctypes
import os
import signal
import time
from ctypes import wintypes

from companion_core.errors import AssistantError


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
                ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]


class ThreadEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("thread_id", wintypes.DWORD),
                ("owner_pid", wintypes.DWORD), ("priority", wintypes.LONG),
                ("delta_priority", wintypes.LONG), ("flags", wintypes.DWORD)]


class ProcessScope:
    def __init__(self):
        self.handle, self.pid, self.process = None, None, None
        self.attached = False
        if os.name != "nt":
            return
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        }
        for name, (args, result) in signatures.items():
            getattr(self.api, name).argtypes = args
            getattr(self.api, name).restype = result
        handle = self.api.CreateJobObjectW(None, None)
        if not handle:
            raise AssistantError("model_process_scope", "无法创建模型进程范围")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if not self.api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.api.CloseHandle(handle)
            raise AssistantError("model_process_scope", "无法限制模型后代进程的生命周期")
        self.handle = handle

    def attach(self, process):
        self.pid, self.process = process.pid, process
        if os.name == "nt" and not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise AssistantError("model_process_scope", "模型进程无法加入生命周期范围")
        self.attached = True

    def resume(self):
        if os.name != "nt":
            return
        # Popen closes CreateProcess's initial thread handle. Enumerate only to
        # reopen this fresh PID's one suspended thread, after Job assignment.
        # CREATE_SUSPENDED prevents the launcher running or spawning before this.
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD.
        if snapshot == ctypes.c_void_p(-1).value:
            raise AssistantError("model_process_scope", "无法定位模型启动线程")
        thread_ids = []
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.owner_pid == self.pid:
                    thread_ids.append(entry.thread_id)
                entry.size = ctypes.sizeof(entry)
                available = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        if not self.attached or len(thread_ids) != 1:
            raise AssistantError("model_process_scope", "模型启动线程或归属范围无法确认")
        thread = self.api.OpenThread(0x2, False, thread_ids[0])  # THREAD_SUSPEND_RESUME.
        if not thread:
            raise AssistantError("model_process_scope", "无法恢复模型启动线程")
        try:
            if self.api.ResumeThread(thread) != 1:
                raise AssistantError("model_process_scope", "模型启动线程未按预期恢复")
        finally:
            self.api.CloseHandle(thread)

    def _capture_members(self, handles):
        # Keep real handles before termination: an empty Job accounting counter
        # does not prove that each process object has finished signalling exit.
        capacity = 32
        while capacity <= 4096:
            class Members(ctypes.Structure):
                _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                            ("pids", ctypes.c_size_t * capacity)]

            members = Members()
            ok = self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(members),
                                                     ctypes.sizeof(members), None)
            if members.assigned > capacity or (not ok and ctypes.get_last_error() == 234):
                capacity = max(capacity * 2, members.assigned)
                continue
            if not ok or members.count != members.assigned:
                raise AssistantError("model_process_scope", "无法核实模型后代进程")
            for pid in members.pids[:members.count]:
                if pid in handles:
                    continue
                handle = self.api.OpenProcess(0x101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION.
                if not handle:
                    if ctypes.get_last_error() == 87:  # Already exited; no process object remains.
                        continue
                    raise AssistantError("model_process_scope", "无法等待模型后代进程退出")
                member = wintypes.BOOL()
                if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                    self.api.CloseHandle(handle)
                    raise AssistantError("model_process_scope", "无法核实模型进程归属")
                if member.value:
                    handles[pid] = handle
                else:
                    self.api.CloseHandle(handle)  # PID reuse must never capture a foreign process.
            return
        raise AssistantError("model_process_scope", "模型后代进程数量超出清理预算")

    def close(self):
        if os.name != "nt":
            if self.pid is not None:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=3)
            return
        if self.handle is None:
            return
        members = {}
        try:
            self._capture_members(members)
            if not self.api.TerminateJobObject(self.handle, 1):
                raise AssistantError("model_process_scope", "无法终止模型进程范围")
            deadline = time.monotonic() + 2
            while True:
                accounting = Accounting()
                if not self.api.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                ):
                    raise AssistantError("model_process_scope", "无法确认模型进程范围已清空")
                if accounting.active:
                    self._capture_members(members)
                    self.api.TerminateJobObject(self.handle, 1)
                waits = [self.api.WaitForSingleObject(handle, 0) for handle in members.values()]
                if any(value not in (0, 258) for value in waits):
                    raise AssistantError("model_process_scope", "模型进程退出检查失败")
                if not accounting.active and all(value == 0 for value in waits):
                    return
                if time.monotonic() >= deadline:
                    raise AssistantError("model_process_scope", "等待模型后代进程退出超时")
                time.sleep(.01)
        finally:
            self.api.TerminateJobObject(self.handle, 1)
            self.api.CloseHandle(self.handle)
            self.handle = None
            for handle in members.values():
                self.api.CloseHandle(handle)
            if self.process is not None:
                if not self.attached and self.process.poll() is None:
                    self.process.kill()  # Assignment failed; the child is still suspended.
                self.process.wait(timeout=3)
