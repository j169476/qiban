"""Exercise worker input, cancellation and cleanup with harmless local children."""
import asyncio
import ctypes
import os
import sys
import time
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from companion_codex.model import CodexModel
from companion_codex.process_scope import ProcessScope
from companion_core.errors import AssistantError


def model_for(tmp_path, source):
    helper = tmp_path / "worker.py"
    helper.write_text(source, encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    processes = []

    def terminate(process):
        processes.append(process)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)

    model = CodexModel(
        command_factory=lambda work, output, schema: [sys.executable, str(helper), str(output)],
        terminator=terminate,
    )
    return model, SimpleNamespace(runtime=runtime), processes


@pytest.mark.asyncio
async def test_nonreading_worker_obeys_total_timeout_without_freezing_loop(tmp_path):
    model, store, processes = model_for(tmp_path, "import time\ntime.sleep(60)\n")
    ticks = []

    async def heartbeat():
        for _ in range(10):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.03)

    ticker = asyncio.create_task(heartbeat())
    started = time.monotonic()
    with pytest.raises(AssistantError) as error:
        await model.generate("test", {"text": "x" * 16000}, {"type": "object"},
                             store=store, policy={"worker_timeout_seconds": 0.4}, guard=lambda: None)
    await ticker
    assert error.value.code == "model_timeout"
    assert time.monotonic() - started < 5
    assert len(ticks) == 10 and ticks[-1] - ticks[0] < 2
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not list((store.runtime / "worker").iterdir())


@pytest.mark.asyncio
async def test_pause_during_stdin_write_stops_worker(tmp_path):
    model, store, processes = model_for(tmp_path, "import time\ntime.sleep(60)\n")
    paused = False

    async def pause():
        nonlocal paused
        await asyncio.sleep(0.1)
        paused = True

    def guard():
        if paused:
            raise AssistantError("run_suspended", "paused")

    task = asyncio.create_task(pause())
    with pytest.raises(AssistantError) as error:
        await model.generate("test", {"text": "x" * 16000}, {"type": "object"},
                             store=store, policy={"worker_timeout_seconds": 30}, guard=guard)
    await task
    assert error.value.code == "run_suspended"
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.asyncio
async def test_task_cancellation_reaps_nonreading_worker(tmp_path):
    model, store, processes = model_for(tmp_path, "import time\ntime.sleep(60)\n")
    task = asyncio.create_task(model.generate("test", {"text": "x" * 16000}, {"type": "object"},
                              store=store, policy={"worker_timeout_seconds": 30}, guard=lambda: None))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.asyncio
async def test_successful_worker_still_validates_structured_result(tmp_path):
    source = "import sys\nfrom pathlib import Path\nsys.stdin.read()\nPath(sys.argv[1]).write_text('{\"ok\":true}')\n"
    model, store, processes = model_for(tmp_path, source)
    result = await model.generate("test", {"text": "x" * 16000}, {"type": "object"},
                                  store=store, policy={"worker_timeout_seconds": 30}, guard=lambda: None)
    assert result == {"ok": True}
    assert processes[0].returncode == 0


@pytest.mark.asyncio
async def test_command_setup_failure_cleans_schema_and_never_starts_child(tmp_path):
    model, store, processes = model_for(tmp_path, "raise AssertionError('must not run')")

    def unavailable(*args):
        raise AssistantError("model_unavailable", "missing CLI")

    model.command_factory = unavailable
    with pytest.raises(AssistantError) as error:
        await model.generate("test", {}, {"type": "object"}, store=store,
                             policy={"worker_timeout_seconds": 30}, guard=lambda: None)
    assert error.value.code == "model_unavailable"
    assert not processes
    assert not list((store.runtime / "worker").iterdir())


@pytest.mark.asyncio
async def test_exited_launcher_with_inherited_stdin_does_not_hold_deadline(tmp_path):
    source = '''import subprocess,sys
subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(4)'], stdin=sys.stdin,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
'''
    model, store, processes = model_for(tmp_path, source)
    started = time.monotonic()
    with pytest.raises(AssistantError) as error:
        await model.generate("test", {"text": "x" * 1_000_000}, {"type": "object"},
                             store=store, policy={"worker_timeout_seconds": .4}, guard=lambda: None)
    assert error.value.code in {"model_failed", "model_timeout"}
    assert time.monotonic() - started < 2
    assert processes[0].poll() is not None
    # The inherited prompt handle is released along with the worker descendants.
    assert not list((store.runtime / "worker").iterdir())


@pytest.mark.asyncio
async def test_terminator_failure_still_cleans_every_worker_file(tmp_path):
    model, store, processes = model_for(tmp_path, "import time\ntime.sleep(60)\n")

    def broken_terminator(process):
        assert process.poll() is not None  # Process scope already reaped it.
        raise OSError("failed callback")

    model.terminator = broken_terminator
    with pytest.raises(OSError):
        await model.generate("test", {}, {"type": "object"}, store=store,
                             policy={"worker_timeout_seconds": .1}, guard=lambda: None)
    assert not list((store.runtime / "worker").iterdir())


def windows_pid_running(pid):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, this test's own child only.
    if not handle:
        return False
    try:
        return api.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT.
    finally:
        api.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows process exit signalling")
def test_job_cleanup_waits_for_members_after_accounting_reaches_zero(monkeypatch):
    scope = ProcessScope()
    waited, closed = [], []
    actual_close = scope.api.CloseHandle
    monkeypatch.setattr(scope, "_capture_members", lambda handles: handles.update({123: 456}))

    def wait(handle, timeout):
        assert (handle, timeout) == (456, 0)
        waited.append(handle)
        return 258 if len(waited) < 3 else 0

    def close(handle):
        closed.append(handle)
        return True if handle == 456 else actual_close(handle)

    monkeypatch.setattr(scope.api, "WaitForSingleObject", wait)
    monkeypatch.setattr(scope.api, "CloseHandle", close)
    scope.close()
    assert len(waited) == 3 and 456 in closed and scope.handle is None


@pytest.mark.skipif(os.name != "nt", reason="Windows job termination failure")
@pytest.mark.parametrize("failure", ["terminate", "accounting", "wait"])
def test_job_cleanup_does_not_silently_accept_failed_exit_checks(monkeypatch, failure):
    scope = ProcessScope()
    if failure == "terminate":
        monkeypatch.setattr(scope.api, "TerminateJobObject", lambda *args: False)
    elif failure == "accounting":
        monkeypatch.setattr(scope, "_capture_members", lambda handles: None)
        monkeypatch.setattr(scope.api, "QueryInformationJobObject", lambda *args: False)
    else:
        actual_close = scope.api.CloseHandle
        monkeypatch.setattr(scope, "_capture_members", lambda handles: handles.update({123: 456}))
        monkeypatch.setattr(scope.api, "WaitForSingleObject", lambda *args: 0xFFFFFFFF)
        monkeypatch.setattr(scope.api, "CloseHandle", lambda handle: True if handle == 456 else actual_close(handle))
    with pytest.raises(AssistantError) as error:
        scope.close()
    assert error.value.code == "model_process_scope" and scope.handle is None


@pytest.mark.skipif(os.name != "nt", reason="Windows job assignment")
@pytest.mark.asyncio
async def test_delayed_job_assignment_cannot_leak_launcher_descendant(tmp_path, monkeypatch):
    pid_file, stop_file = tmp_path / "child.pid", tmp_path / "child.stop"
    child = """import sys,time
from pathlib import Path
deadline = time.monotonic() + 10
while not Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(.05)
"""
    source = f"""import subprocess,sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', {child!r}, {str(stop_file)!r}], stdin=sys.stdin,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
Path({str(pid_file)!r}).write_text(str(child.pid))
"""
    model, store, processes = model_for(tmp_path, source)
    original, ran_before_attach = ProcessScope.attach, []

    def delayed_attach(self, process):
        time.sleep(.35)  # The controlling thread may be descheduled after CreateProcess.
        ran_before_attach.append(pid_file.exists())
        original(self, process)

    monkeypatch.setattr(ProcessScope, "attach", delayed_attach)
    try:
        with pytest.raises(AssistantError) as error:
            await model.generate("test", {}, {"type": "object"}, store=store,
                                 policy={"worker_timeout_seconds": 2}, guard=lambda: None)
        assert error.value.code == "model_failed"  # Launcher intentionally writes no result.
        assert ran_before_attach == [False]
        assert pid_file.exists()
        assert not windows_pid_running(int(pid_file.read_text()))
        assert processes[0].poll() is not None
        assert not list((store.runtime / "worker").iterdir())
    finally:
        stop_file.touch()  # Cooperative cleanup even when a regression escapes the Job.
        if pid_file.exists():
            for _ in range(30):
                if not windows_pid_running(int(pid_file.read_text())):
                    break
                await asyncio.sleep(.05)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process cleanup")
@pytest.mark.asyncio
@pytest.mark.parametrize("api_name", ["AssignProcessToJobObject", "ResumeThread"])
async def test_job_attach_or_resume_failure_never_executes_worker(tmp_path, monkeypatch, api_name):
    marker = tmp_path / "executed"
    model, store, processes = model_for(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    original, scopes = ProcessScope.__init__, []

    def faulted_scope(self):
        original(self)
        scopes.append(self)
        failure = False if api_name == "AssignProcessToJobObject" else 0xFFFFFFFF
        setattr(self.api, api_name, lambda *args: failure)

    monkeypatch.setattr(ProcessScope, "__init__", faulted_scope)
    with pytest.raises(AssistantError) as error:
        await model.generate("test", {}, {"type": "object"}, store=store,
                             policy={"worker_timeout_seconds": 2}, guard=lambda: None)
    assert error.value.code == "model_process_scope"
    assert not marker.exists()
    assert len(processes) == 1 and processes[0].poll() is not None
    assert len(scopes) == 1 and scopes[0].handle is None
    assert not list((store.runtime / "worker").iterdir())
