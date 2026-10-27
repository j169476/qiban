import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_qq.config import validate
from companion_qq.lifecycle import ManagedBridge
from tests_postgres.test_qq_module import OPTIONS


class Store:
    def __init__(self):
        self.values = {}

    def set_state(self, key, value):
        self.values[key] = value


def bridge(tmp_path, monkeypatch, outcomes, *, real=False):
    options = validate(OPTIONS | {"launch": {"executable": sys.executable, "arguments": ["-c", "import time; time.sleep(60)"],
                                           "directory": str(tmp_path), "timeout": 5}})
    scopes, started = [], []

    class Scope:
        def __init__(self):
            self.closed = False
            scopes.append(self)

        def attach(self, process):
            self.process = process

        def resume(self):
            pass

        def close(self):
            self.closed = True

    def spawn(*args, **kwargs):
        started.append((args, kwargs))
        return SimpleNamespace(pid=12345)

    manager = ManagedBridge(Store(), options, **({} if real else {"process_factory": spawn, "scope_factory": Scope}))
    monkeypatch.setattr(manager, "port_open", lambda: False)

    def probe():
        result = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(result, str):
            raise AssistantError(result, "synthetic")
        return result

    return manager, SimpleNamespace(probe=probe), scopes, started


@pytest.mark.asyncio
async def test_healthy_external_bridge_is_reused_and_never_stopped(tmp_path, monkeypatch):
    manager, provider, scopes, started = bridge(tmp_path, monkeypatch, [{"account": "10001"}])
    manager.store.set_state(manager.key, {"pid": 12345, "status": "starting"})  # stale or reused PID
    await manager.ensure(provider, lambda: None)
    manager.stop()
    assert not scopes and not started


@pytest.mark.asyncio
async def test_explicit_launch_and_repeated_stop_cleanup_owned_scope(tmp_path, monkeypatch):
    manager, provider, scopes, started = bridge(tmp_path, monkeypatch, ["qq_provider_unavailable", {"account": "10001"}])
    await manager.ensure(provider, lambda: None)
    assert len(started) == 1 and manager.state == "connecting"
    manager.stop()
    manager.stop()
    assert scopes[0].closed and manager.scope is None
    assert manager.store.values[manager.key]["status"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["qq_account_mismatch", "qq_target_unverified"])
async def test_wrong_account_rolls_back_only_owned_bridge(tmp_path, monkeypatch, failure):
    manager, provider, scopes, _ = bridge(tmp_path, monkeypatch, ["qq_provider_unavailable", failure])
    with pytest.raises(AssistantError) as error:
        await manager.ensure(provider, lambda: None)
    assert error.value.code == failure and scopes[0].closed


@pytest.mark.asyncio
async def test_port_conflict_does_not_spawn_or_kill(tmp_path, monkeypatch):
    manager, provider, scopes, started = bridge(tmp_path, monkeypatch, ["qq_provider_unavailable"])
    monkeypatch.setattr(manager, "port_open", lambda: True)
    with pytest.raises(AssistantError) as error:
        await manager.ensure(provider, lambda: None)
    assert error.value.code == "qq_port_conflict" and not scopes and not started


@pytest.mark.asyncio
async def test_stop_during_startup_closes_owned_job(tmp_path, monkeypatch):
    manager, provider, scopes, _ = bridge(tmp_path, monkeypatch, ["qq_provider_unavailable"])
    calls = []

    def guard():
        calls.append(1)
        if len(calls) > 1:
            raise AssistantError("run_suspended", "synthetic stop")

    with pytest.raises(AssistantError):
        await manager.ensure(provider, guard)
    assert scopes[0].closed


@pytest.mark.asyncio
async def test_real_process_handle_cleanup(tmp_path, monkeypatch):
    manager, provider, _, _ = bridge(tmp_path, monkeypatch, ["qq_provider_unavailable", {"account": "10001"}], real=True)
    try:
        await manager.ensure(provider, lambda: None)
        process = manager.process
        assert process.poll() is None and Path(sys.executable).is_file()
        manager.stop()
        assert process.poll() is not None
    finally:
        manager.stop()


@pytest.mark.asyncio
async def test_three_owned_restart_cycles_release_process_and_listening_port(tmp_path):
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    source = ('import socket,time; s=socket.socket(); '
              f's.bind(("127.0.0.1",{port})); s.listen(); time.sleep(60)')
    options = validate(OPTIONS | {"http": f"http://127.0.0.1:{port}", "launch": {
        "executable": sys.executable, "arguments": ["-c", source],
        "directory": str(tmp_path), "timeout": 5}})
    manager = ManagedBridge(Store(), options)

    def probe():
        if manager.port_open():
            return {"account": options["account"]}
        raise AssistantError("qq_provider_unavailable", "synthetic listener is not ready")

    try:
        for _ in range(3):
            await manager.ensure(SimpleNamespace(probe=probe), lambda: None)
            process = manager.process
            assert process.poll() is None and manager.port_open()
            manager.stop()
            assert process.poll() is not None and manager.scope is None
            assert not manager.port_open()
            assert manager.store.values[manager.key]["port_still_open"] is False
    finally:
        manager.stop()


@pytest.mark.asyncio
async def test_login_wait_reuses_external_process_without_ownership(tmp_path, monkeypatch):
    manager, provider, scopes, started = bridge(tmp_path, monkeypatch, ['qq_login_required', {'account': '10001'}])
    await manager.ensure(provider, lambda: None)
    manager.stop()
    assert not scopes and not started


@pytest.mark.asyncio
async def test_start_timeout_closes_owned_process_group(tmp_path, monkeypatch):
    import time

    import companion_qq.lifecycle as lifecycle

    manager, provider, scopes, _ = bridge(tmp_path, monkeypatch, ['qq_provider_unavailable'])
    ticks = iter([0, 6])
    monkeypatch.setattr(lifecycle, 'time', SimpleNamespace(monotonic=lambda: next(ticks), time=time.time))
    with pytest.raises(AssistantError) as error:
        await manager.ensure(provider, lambda: None)
    assert error.value.code == 'qq_launch_timeout' and scopes[0].closed
    assert manager.state == 'failed' and manager.scope is None
