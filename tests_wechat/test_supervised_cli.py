"""Composition tests use fake ports; no live host, model, DB or UI is opened."""
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_wechat import supervised_cli as cli

THREAD = "12345678-1234-1234-1234-123456789abc"


def options(**changes):
    values = {"project": Path.cwd(), "locations": Path("synthetic-locations.json"),
              "targets": Path("synthetic-targets.json"), "worker_python": None,
              "duration_seconds": 180, "poll_seconds": 1.0}
    return SimpleNamespace(**{**values, **changes})


def test_public_status_is_full_projection_and_never_forwards_private_data():
    value = cli.public_status({"status": "ready", "handled": 1, "succeeded": 1, "role_stopped": False,
        "text": "SYNTHETIC_SECRET", "account_identity": "SYNTHETIC_SECRET", "code": "SYNTHETIC_SECRET",
        "elapsed_seconds": float("nan"), "test": {"status": "active", "nonce": "XLD-ROLE-" + "A" * 24,
        "deadline_at": "2026-10-24T04:00:00+00:00", "target": "SYNTHETIC_SECRET",
        "admitted": {"owner_main": 1, "family": 99, "SYNTHETIC_SECRET": 1}}})
    assert "SYNTHETIC_SECRET" not in json.dumps(value)
    assert value["handled"] == value["succeeded"] == 1
    assert value["elapsed_seconds"] == 0.0
    assert value["test"]["admitted"] == {"owner_main": 1, "family": 0}
    assert value["test"]["nonce"] == "XLD-ROLE-" + "A" * 24


@pytest.mark.parametrize("field,value", [("duration_seconds", 601), ("duration_seconds", 4),
                                        ("duration_seconds", True), ("poll_seconds", float("inf")),
                                        ("poll_seconds", 0.1)])
def test_invalid_timing_is_rejected_before_role_start(monkeypatch, field, value):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    with pytest.raises(AssistantError) as exc:
        cli.validate_options(options(**{field: value}))
    assert exc.value.code == "wechat_role_configuration"


def test_requires_real_inherited_thread_environment_without_override_argument(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(AssistantError) as exc:
        cli.validate_options(options())
    assert exc.value.code == "host_identity_missing"
    with pytest.raises(AssistantError):
        cli.parser().parse_args(["--locations", "a", "--targets", "b", "--thread-id", THREAD])
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    cli.validate_options(options(duration_seconds=600))


def test_model_effort_is_explicit_and_bounded(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    args = cli.parser().parse_args(["--locations", "a", "--targets", "b", "--model-effort", "low",
                                    "--model", "synthetic-model"])
    cli.validate_options(args)
    assert args.model_effort == "low"
    assert args.model == "synthetic-model"
    assert cli.parser().parse_args(["--locations", "a", "--targets", "b"]).model_effort is None
    with pytest.raises(AssistantError):
        cli.validate_options(options(model_effort="invalid"))
    with pytest.raises(AssistantError):
        cli.validate_options(options(model="invalid\nmodel"))


def test_real_child_stdio_emits_ready_before_any_source_access_then_exits_on_eof(tmp_path):
    # Invalid, nonexistent source paths prove the handshake happens before any
    # source access. This synthetic controller is never used to start a role:
    # the parent deliberately sends no successful handshake response.
    child = subprocess.Popen([sys.executable, "-u", "-m", "companion_wechat.supervised_cli",
                              "--locations", str(tmp_path / "does-not-exist.json"),
                              "--targets", str(tmp_path / "also-does-not-exist.json")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env={**os.environ, "CODEX_THREAD_ID": THREAD},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: messages.put(child.stdout.readline(16385)), daemon=True).start()
    try:
        frame = json.loads(messages.get(timeout=8))
        assert set(frame) == {"v", "type", "id", "protocol"}
        assert frame["v"] == 1 and frame["type"] == "ready"
        assert frame["protocol"] == "wechat-supervised-v1"
        child.stdin.close()
        assert child.wait(timeout=8) == 2
    finally:
        if not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=8)
        child.stdout.close()


class Pipe:
    def __init__(self):
        self.frames, self.handshakes, self.closed = [], 0, False

    async def handshake(self):
        self.handshakes += 1

    async def supervise(self, coroutine):
        return await coroutine

    def publish(self, kind, payload):
        self.frames.append((kind, payload))

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_run_assembles_real_port_classes_and_cleans_existing_store_host(monkeypatch):
    import companion_codex.host
    import companion_codex.model
    import companion_wechat.receiver

    calls = []
    store = SimpleNamespace()
    model = object()

    @contextmanager
    def open_store(project):
        assert project == Path.cwd()
        calls.append("store-open")
        try:
            yield store, "synthetic-existing-collector-account"
        finally:
            calls.append("store-close")

    class Host:
        def __init__(self, project, *, active_cache_seconds):
            assert project == Path.cwd()
            assert active_cache_seconds == 0.1
            self.thread_id = THREAD
            calls.append("host")

        def close(self):
            calls.append("host-close")

    class Receiver:
        def __init__(self, selected, config, *, account, **kwargs):
            assert selected is store and config == {"synthetic": True} and not kwargs
            self.account = account
            calls.append("receiver")

    async def run(receiver, selected_model, host, pipe, **kwargs):
        assert receiver.account == "synthetic-existing-collector-account"
        assert selected_model is model and host.thread_id == THREAD and pipe.handshakes == 1
        assert kwargs == {"duration_seconds": 180, "poll_seconds": 1.0}
        calls.append("run")
        return {"status": "complete", "role_stopped": True}

    monkeypatch.setattr(cli, "open_collector_store", open_store)
    monkeypatch.setattr(cli, "load_config", lambda *_args: {"synthetic": True})
    monkeypatch.setattr(cli, "run_supervised", run)
    monkeypatch.setattr(companion_codex.host, "CodexHost", Host)
    monkeypatch.setattr(companion_codex.model, "CodexModel", lambda: model)
    monkeypatch.setattr(companion_wechat.receiver, "DatabaseGateway", Receiver)
    result = await cli._run(options(), Pipe())
    assert result["status"] == "complete"
    assert calls == ["store-open", "host", "receiver", "run", "host-close", "store-close"]


@pytest.mark.asyncio
async def test_plaintext_store_is_rejected_and_source_host_are_still_closed(monkeypatch):
    closed = []

    async def close_source():
        closed.append("receiver")

    receiver = SimpleNamespace(store=SimpleNamespace(backend="sqlite", db_path="synthetic"), close=close_source)
    host = SimpleNamespace(close=lambda: closed.append("host"))
    with pytest.raises(AssistantError) as exc:
        await cli.run_supervised(receiver, None, host, Pipe())
    assert exc.value.code == "wechat_send_store_unencrypted"
    assert closed == ["receiver", "host"]


@pytest.mark.asyncio
async def test_inherited_controller_mismatch_closes_before_source_start(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    closed = []

    async def close_source():
        closed.append("receiver")

    receiver = SimpleNamespace(store=SimpleNamespace(backend="postgres", db_path=None), close=close_source)
    host = SimpleNamespace(identity=lambda: "different-controller", close=lambda: closed.append("host"))
    with pytest.raises(AssistantError) as exc:
        await cli.run_supervised(receiver, None, host, Pipe())
    assert exc.value.code == "host_identity_missing"
    assert closed == ["receiver", "host"]


@pytest.mark.asyncio
async def test_stdio_hides_raw_error_and_always_closes_channel(monkeypatch):
    pipe = Pipe()

    async def fail(*_args):
        raise RuntimeError("SYNTHETIC_PRIVATE_MODEL_OUTPUT")

    monkeypatch.setattr(cli, "PrivateJsonPipe", lambda *_args: pipe)
    monkeypatch.setattr(cli, "_run", fail)
    assert await cli._stdio(options(), None, None) == 2
    assert pipe.closed
    assert "SYNTHETIC_PRIVATE" not in json.dumps(pipe.frames)
    assert pipe.frames[-1][1]["report"]["code"] == "wechat_supervised_failed"


@pytest.mark.asyncio
async def test_stdio_cancellation_does_not_swallow_shutdown(monkeypatch):
    pipe = Pipe()

    async def cancel(*_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cli, "PrivateJsonPipe", lambda *_args: pipe)
    monkeypatch.setattr(cli, "_run", cancel)
    with pytest.raises(asyncio.CancelledError):
        await cli._stdio(options(), None, None)
    assert pipe.closed and pipe.frames == []


@pytest.mark.asyncio
async def test_model_error_preserves_completed_cleanup_and_public_counters(monkeypatch):
    pipe = Pipe()
    pipe.last_public_report = {"status": "blocked", "role_stopped": True,
                               "handled": 0, "elapsed_seconds": 3.0}

    async def fail(*_args):
        raise AssistantError("model_output_size", "SYNTHETIC_PRIVATE_DETAILS")

    monkeypatch.setattr(cli, "PrivateJsonPipe", lambda *_args: pipe)
    monkeypatch.setattr(cli, "_run", fail)
    assert await cli._stdio(options(), None, None) == 2
    result = pipe.frames[-1][1]["report"]
    assert result["role_stopped"] is True and result["elapsed_seconds"] == 3.0
    assert result["code"] == "model_output_size" and "SYNTHETIC_PRIVATE" not in json.dumps(pipe.frames)
