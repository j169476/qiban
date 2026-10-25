"""Regression gates for verified P0 fixes, not a claim of complete isolation."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from companion_codex.model import worker_command
from companion_codex.native import command_profile


def probe_module():
    spec = importlib.util.spec_from_file_location(
        "p0_probe", Path(__file__).resolve().parents[1] / "scripts/probe_codex_isolation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", [None, "web", "image"])
def test_native_profiles_cannot_reenable_arbitrary_local_image_reads(tmp_path, monkeypatch, kind):
    # Explicit tools.view_image can otherwise override a feature-level default.
    (tmp_path / "config.toml").write_text(
        '[tools]\nview_image=true\n[features]\nview_image=true\nhooks=true\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("companion_codex.model.shutil.which", lambda _: sys.executable)
    image = tmp_path / "broker-approved.png"
    command = worker_command(tmp_path, tmp_path / "result", tmp_path / "schema", [image])
    if kind:
        command = command_profile(command, kind)
    assert "tools.view_image=false" in command
    assert command[command.index("view_image") - 1] == "--disable"
    assert command[command.index("hooks") - 1] == "--disable"
    assert "project_doc_max_bytes=0" in command
    # Normal image inputs still arrive explicitly; this is distinct from a tool
    # which lets a model select arbitrary local paths.
    assert command[command.index("--image") + 1] == str(image)


@pytest.mark.parametrize("code,stdout,stderr,status,accepted", [
    (1, "", "Restricted read-only access requires the elevated Windows sandbox backend", "not_executed", False),
    (0, "model says access was denied", "", "not_executed", False),
    (0, json.dumps({"executed": True, "outside_read": False}), "", "not_executed", False),
    (0, json.dumps({"executed": True, "outside_read": "false", "outside_write": False,
                    "loopback_connect": False}), "", "not_executed", False),
    (0, json.dumps({"executed": True, "outside_read": True, "outside_write": False,
                    "loopback_connect": False}), "", "executed", False),
    (0, json.dumps({"executed": True, "outside_read": False, "outside_write": False,
                    "loopback_connect": True}), "", "executed", False),
    (0, json.dumps({"executed": True, "outside_read": False, "outside_write": False,
                    "loopback_connect": False}), "", "executed", True),
    (1, json.dumps({"executed": True, "outside_read": False, "outside_write": False,
                    "loopback_connect": False}), "", "execution_failed", False),
])
def test_probe_cannot_misreport_startup_failure_or_partial_result_as_isolation(
        code, stdout, stderr, status, accepted):
    result = probe_module().decode_probe(code, stdout, stderr)
    assert result["status"] == status
    assert result["isolation_passed"] is accepted
    if "elevated" in stderr:
        assert result["diagnostic"] == "requires_elevated_windows_backend"
