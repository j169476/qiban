import asyncio
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from companion_codex.host import CodexHost
from companion_core.errors import AssistantError
from companion_core.operation_context import Trace, bounded, detached_trace, stage
from companion_service import bridge


def test_active_chat_does_not_scan_archive(tmp_path):
    calls = []

    class Host(CodexHost):
        def request(self, method, params):
            calls.append((method, params))
            if method == "thread/read":
                return {"thread": {"cwd": str(tmp_path)}}
            assert params["archived"] is False
            return {"data": [{"id": "synthetic"}], "nextCursor": None}

    assert Host(tmp_path).require_active("synthetic")["archived"] is False
    assert len(calls) == 2


def test_host_pagination_deadline_and_repeated_cursor_fail_closed(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])

    class Host(CodexHost):
        def request(self, method, params):
            now[0] += 5
            if method == "thread/read":
                return {"thread": {"cwd": str(tmp_path)}}
            return {"data": [], "nextCursor": str(now[0])}

    with pytest.raises(AssistantError) as error:
        Host(tmp_path).require_active("synthetic")
    assert error.value.code == "host_timeout" and now[0] == 15
    now[0] = 0
    host = Host(tmp_path)
    monkeypatch.setattr(host, "request", lambda method, params: {"thread": {"cwd": str(tmp_path)}}
                        if method == "thread/read" else {"data": [], "nextCursor": "repeat"})
    with pytest.raises(AssistantError) as error:
        host.status("synthetic")
    assert error.value.code == "host_state_unknown"


def test_service_readiness_has_wall_clock_budget(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(bridge, "START_SECONDS", 5)

    def offline(_):
        now[0] += 1
        return False

    monkeypatch.setattr(bridge, "ready", offline)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(AssistantError) as error:
        bridge.ensure_service({"data": str(tmp_path), "project": str(tmp_path)})
    assert error.value.code == "service_start_timeout" and now[0] < 7


@pytest.mark.parametrize("failure,code", [(PermissionError, "execution_denied"), (OSError, "service_launch_failed")])
def test_spawn_failures_are_distinguished(tmp_path, monkeypatch, failure, code):
    monkeypatch.setattr(bridge, "ready", lambda _: False)

    def spawn(*args, **kwargs):
        raise failure("sensitive-command-and-path")

    monkeypatch.setattr(bridge.subprocess, "Popen", spawn)
    with pytest.raises(AssistantError) as error:
        bridge.ensure_service({"data": str(tmp_path), "project": str(tmp_path)})
    assert error.value.code == code and "sensitive" not in str(error.value)


def test_startup_failure_includes_only_this_process_safe_stages(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    status = runtime / "service-startup.json"
    status.write_text(json.dumps({"pid": 123, "secret": "private", "stages": [
        {"stage": "database_recovery", "status": "failed", "code": "database_timeout", "message": "private"},
        {"stage": "private", "status": "failed"}]}))
    process = SimpleNamespace(pid=123, poll=lambda: 2)
    monkeypatch.setattr(bridge, "ready", lambda _: False)
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(AssistantError) as error:
        bridge.ensure_service({"data": str(tmp_path), "project": str(tmp_path)})
    assert error.value.code == "service_start_failed"
    assert error.value.startup == {"exit_code": 2, "startup_stages": [
        {"stage": "database_recovery", "status": "failed", "code": "database_timeout"}]}
    assert bridge.startup_state({"data": str(tmp_path)}, SimpleNamespace(pid=456)) == {}
    status.write_text("x" * 65537)
    assert bridge.startup_state({"data": str(tmp_path)}, process) == {}


def test_database_lock_contention_has_distinct_error(tmp_path, monkeypatch):
    from filelock import Timeout

    from companion_service import managed_postgres

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "Companion" / "synthetic"

    def busy(*args, **kwargs):
        raise Timeout("synthetic.lock")

    monkeypatch.setattr(managed_postgres, "FileLock", busy)
    with pytest.raises(AssistantError) as error:
        managed_postgres.ensure({"id": "synthetic", "postgres_runtime": str(root)})
    assert error.value.code == "database_start_busy"


def test_timeout_reports_unknown_result_and_sanitized_stage(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "metadata", lambda _: {"ports": {"admin": 45180}})
    monkeypatch.setattr(bridge, "ensure_service", lambda _: None)
    monkeypatch.setattr(bridge, "secrets_for", lambda _: {"bridge_secret": "secret-bridge-value"})

    def post(*args, **kwargs):
        assert kwargs["timeout"] <= bridge.CONTROL_SECONDS
        raise httpx.ReadTimeout("secret-request-body")

    monkeypatch.setattr(bridge.httpx, "post", post)
    with pytest.raises(AssistantError) as error:
        bridge.call("private-project", "management_open", {}, thread="private-thread")
    value = error.value.as_dict()
    assert value["error"] == "service_control_timeout"
    assert value["diagnostics"]["stages"][-1]["stage"] == "control_request"
    output = capsys.readouterr().err + json.dumps(value)
    assert all(secret not in output for secret in ("private-project", "private-thread", "secret-bridge-value", "secret-request-body"))


def test_server_verification_diagnostic_survives_cli_bridge(monkeypatch):
    monkeypatch.setattr(bridge, "metadata", lambda _: {"ports": {"admin": 45180}})
    monkeypatch.setattr(bridge, "ensure_service", lambda _: None)
    monkeypatch.setattr(bridge, "secrets_for", lambda _: {"bridge_secret": "synthetic"})
    monkeypatch.setattr(bridge.httpx, "post", lambda *args, **kwargs: SimpleNamespace(status_code=403,
        json=lambda: {"error": "host_timeout", "message": "safe", "diagnostics": {
            "stages": [{"stage": "host_verification", "status": "failed"}]}}))
    with pytest.raises(AssistantError) as error:
        bridge.call("synthetic", "management_open", {}, thread="synthetic")
    assert error.value.as_dict()["diagnostics"]["server"]["stages"][0]["stage"] == "host_verification"


def test_trace_and_deadline_restore_between_requests(capsys):
    trace = Trace("test")
    with pytest.raises(AssistantError):
        with trace.activate(), bounded(0, "test_timeout"), stage("safe_stage"):
            pytest.fail("expired")
    with stage("separate_request"):
        pass
    assert "separate_request" not in capsys.readouterr().err


def test_closed_progress_reader_does_not_abort_operation(monkeypatch):
    def closed(*args, **kwargs):
        raise BrokenPipeError("reader closed")

    monkeypatch.setattr("builtins.print", closed)
    trace = Trace("test")
    with trace.activate(), stage("completed_operation"):
        pass
    assert trace.snapshot()["stages"][0]["status"] == "completed"


def test_background_tasks_do_not_inherit_startup_trace():
    trace = Trace("startup", emit=False)

    async def background():
        await asyncio.sleep(0)
        with stage("background_poll"):
            pass

    async def startup():
        with trace.activate(), stage("service_open"), detached_trace():
            task = asyncio.create_task(background())
        await task

    asyncio.run(startup())
    assert [row["stage"] for row in trace.snapshot()["stages"]] == ["service_open"]


@pytest.mark.parametrize("mode", ["timeout", "denied", "serve"])
def test_plugin_launcher_bounds_control_without_timing_out_mcp(tmp_path, monkeypatch, capsys, mode):
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "scripts/run.py"
    script.parent.mkdir()
    shutil.copy(root / "plugins/qiban/scripts/run.py", script)
    (tmp_path / "runtime.json").write_text(json.dumps({"python": sys.executable, "project": str(tmp_path), "data": str(tmp_path)}))
    monkeypatch.setattr(sys, "argv", [str(script), "serve" if mode == "serve" else "call", "service_resume"])

    def invoke(args, **kwargs):
        assert kwargs["timeout"] == (None if mode == "serve" else 120)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(args, 120)
        if mode == "denied":
            raise PermissionError("sensitive-runtime-path")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", invoke)
    with pytest.raises(SystemExit) as exit_value:
        runpy.run_path(str(script), run_name="__main__")
    output = capsys.readouterr().out
    assert exit_value.value.code == (0 if mode == "serve" else 2)
    assert "sensitive-runtime-path" not in output
    if mode != "serve":
        assert json.loads(output)["error"] == ("plugin_control_timeout" if mode == "timeout" else "execution_denied")
