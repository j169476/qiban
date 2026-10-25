from types import SimpleNamespace

import pytest

from companion_codex.host import CodexHost
from companion_core.errors import AssistantError


def test_open_desktop_uses_fixed_command_and_never_submits_a_turn(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("companion_codex.host.shutil.which", lambda name: "codex.exe")
    monkeypatch.setattr("companion_codex.host.subprocess.run", lambda args, **kwargs:
                        calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    host = CodexHost(tmp_path)
    assert host.open_desktop() == {"launch_requested": True, "role_started": False}
    assert calls[0][0] == ["codex.exe", "app", str(tmp_path.resolve())]
    assert calls[0][1]["timeout"] == 20


def test_open_desktop_reports_failure_without_claiming_online(tmp_path, monkeypatch):
    monkeypatch.setattr("companion_codex.host.shutil.which", lambda name: None)
    with pytest.raises(AssistantError) as error:
        CodexHost(tmp_path).open_desktop()
    assert error.value.code == "host_unavailable"


def test_desktop_check_precedes_cached_thread_status(tmp_path):
    def stopped():
        raise AssistantError("host_stopped", "stopped")
    host = CodexHost(tmp_path, desktop=SimpleNamespace(require_online=stopped))
    with pytest.raises(AssistantError) as error:
        host.require_active("synthetic")
    assert error.value.code == "host_stopped"
