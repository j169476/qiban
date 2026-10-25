import json
import subprocess
from types import SimpleNamespace

import pytest

from companion_wechat.visual_cli import failure, isolated_visual_probe, main


def worker(report=None, code=0, stderr="synthetic private data"):
    return lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=json.dumps(
        report or failure("ok")), stderr=stderr)


def test_worker_is_subprocess_with_timeout_and_private_input_on_stdin():
    def run(command, **kwargs):
        assert command[-2:] == ["companion_wechat.visual_cli", "_worker"]
        assert "private.png" not in str(command)
        assert json.loads(kwargs["input"])["image_path"] == "private.png"
        assert kwargs["capture_output"] and kwargs["timeout"] == 4
        return worker()()

    assert isolated_visual_probe("private.png", timeout=4, runner=run)["status"] == "ok"


@pytest.mark.parametrize("change", [
    {"recognized_text_exported": True}, {"image_exported": True}, {"ui_accessed": True},
    {"messages_sent": 1}, {"mode": "live"}, {"text_redacted": False},
    {"status": "unknown"}, {"secret": "synthetic private data"}, {"schema_version": True},
])
def test_rejects_invalid_worker_protocol_without_echo(change):
    result = isolated_visual_probe("private.png", runner=worker({**failure("ok"), **change}))
    assert result["status"] == "visual_worker_failed"
    assert "synthetic private" not in json.dumps(result)


def test_timeout_does_not_export_child_stdout_or_stderr():
    def expired(*args, **kwargs):
        raise subprocess.TimeoutExpired("worker", 1, output="secret text", stderr="secret stderr")

    result = isolated_visual_probe("private.png", runner=expired)
    assert result["status"] == "visual_probe_timeout" and "secret" not in json.dumps(result)


def test_process_failure_is_redacted():
    result = isolated_visual_probe("private.png", runner=worker(code=1))
    assert result["status"] == "visual_worker_failed"


def test_cannot_overwrite_input_image_with_report(tmp_path, capsys):
    image = tmp_path / "sample.png"
    image.write_bytes(b"synthetic-original")
    assert main(["--image", str(image), "--output", str(image)]) == 2
    assert image.read_bytes() == b"synthetic-original"
    assert json.loads(capsys.readouterr().out)["status"] == "visual_configuration_error"


def test_cli_writes_redacted_report(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("companion_wechat.visual_cli.isolated_visual_probe", lambda *a, **k: failure("ok"))
    report = tmp_path / "report.json"
    assert main(["--image", "synthetic.png", "--output", str(report)]) == 0
    assert json.loads(report.read_text()) == json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("timeout", ["0", "61", "nan", "inf"])
def test_invalid_deadline_is_rejected(timeout, capsys):
    assert main(["--image", "synthetic.png", "--timeout", timeout]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "visual_configuration_error"


def test_root_cli_routes_visual_probe_without_desktop_sdk(monkeypatch):
    from companion_wechat.__main__ import main as root_main

    monkeypatch.setattr("companion_wechat.visual_cli.main", lambda args: 37 if args == ["--help"] else 1)
    assert root_main(["visual-probe", "--help"]) == 37
