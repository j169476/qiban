"""Explicit monitor recovery using synthetic child processes and heartbeat files."""
import time
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_service import watchdog


class Child:
    def __init__(self, exited=False):
        self.exited, self.terminated = exited, False

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout):
        if not self.exited:
            raise watchdog.subprocess.TimeoutExpired("synthetic", timeout)
        return 0

    def terminate(self):
        self.terminated = self.exited = True


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "ProcessHandle", lambda pid: SimpleNamespace(born=42, close=lambda: None))
    service = SimpleNamespace(store=SimpleNamespace(runtime=tmp_path), maintenance_until=0)
    return watchdog.Supervisor(service, tmp_path)


@pytest.mark.parametrize("old_alive", [False, True])
def test_recovery_replaces_own_child_and_waits_for_new_instance(supervisor, monkeypatch, old_alive):
    previous = supervisor.instance
    old = supervisor.process = Child(exited=not old_alive)
    watchdog.write(supervisor.store.runtime / "service-watchdog.json", {"instance": previous, "at": 0})
    children = []

    def spawn(*args, **kwargs):
        child = Child()
        children.append(child)
        assert supervisor.instance != previous
        watchdog.write(supervisor.store.runtime / "service-watchdog.json",
                       {"instance": supervisor.instance, "at": time.time()})
        return child

    monkeypatch.setattr(watchdog.subprocess, "Popen", spawn)
    supervisor.ensure_ready(timeout=0)
    assert len(children) == 1 and supervisor.status()["healthy"]
    assert old.terminated is old_alive
    supervisor.ensure_ready(timeout=0)
    assert len(children) == 1  # A healthy monitor is reused.


def test_stale_heartbeat_cannot_make_new_child_ready(supervisor, monkeypatch):
    previous = supervisor.instance
    supervisor.process = Child(exited=True)
    watchdog.write(supervisor.store.runtime / "service-watchdog.json", {"instance": previous, "at": time.time()})
    child = Child()
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda *args, **kwargs: child)
    with pytest.raises(AssistantError) as error:
        supervisor.ensure_ready(timeout=0)
    assert error.value.code == "supervisor_lost"
    assert not supervisor.status()["healthy"] and child.terminated and supervisor.closed
