from types import SimpleNamespace

import pytest

from companion_codex.host import CodexHost
from companion_core.errors import AssistantError


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-controller")
    now = [100.0]
    monkeypatch.setattr("companion_codex.host.time.monotonic", lambda: now[0])
    value = CodexHost(tmp_path, active_cache_seconds=0.1)
    value.process = SimpleNamespace(poll=lambda: None)
    calls = []

    def status(target):
        calls.append(target)
        return {"archived": False, "thread": target}

    monkeypatch.setattr(value, "status", status)
    return value, calls, now


def test_opt_in_coalesces_only_same_live_host_and_returns_copy(host):
    value, calls, now = host
    value.require_active()["archived"] = True
    assert value.require_active()["archived"] is False
    assert len(calls) == 1
    value.require_active("different")
    assert len(calls) == 2
    now[0] += 0.101
    value.require_active("different")
    assert len(calls) == 3


def test_expired_snapshot_never_hides_revocation_or_failure(host, monkeypatch):
    value, _calls, now = host
    value.require_active()
    now[0] += 0.101
    monkeypatch.setattr(value, "status", lambda _: {"archived": True})
    with pytest.raises(AssistantError) as error:
        value.require_active()
    assert error.value.code == "chat_archived" and value._active_snapshot is None
    monkeypatch.setattr(value, "status", lambda _: (_ for _ in ()).throw(AssistantError("host_unavailable", "secret")))
    with pytest.raises(AssistantError) as error:
        value.require_active()
    assert error.value.code == "host_unavailable" and value._active_snapshot is None


def test_dead_process_or_disabled_cache_always_rechecks(host):
    value, calls, _now = host
    value.require_active()
    value.process = SimpleNamespace(poll=lambda: 1)
    value.require_active()
    assert len(calls) == 2
    value.process = SimpleNamespace(poll=lambda: None)
    value.active_cache_seconds = 0
    value.require_active()
    value.require_active()
    assert len(calls) == 4
    value.process = None
    value.close()
    assert value._active_snapshot is None


def test_default_remains_uncached_and_larger_windows_are_rejected(tmp_path):
    assert CodexHost(tmp_path).active_cache_seconds == 0
    for duration in (0.101, -1, float("nan"), True):
        with pytest.raises(AssistantError):
            CodexHost(tmp_path, active_cache_seconds=duration)
