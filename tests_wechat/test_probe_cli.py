"""CLI and process protocol tests; no real UI or child process is used."""
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from companion_wechat import __main__ as cli

SAFE = {"schema_version": 1, "status": "ui_visible_identity_unverified", "read_only": True,
        "messages_sent": 0}
TARGET = {"alias": "friend", "kind": "private", "title": "SENSITIVE synthetic title"}


def test_loads_exact_target_specs_without_rewriting_titles(tmp_path):
    path = tmp_path / "targets.json"
    values = [TARGET, {"alias": "family", "kind": "group", "title": "合成测试群"}]
    path.write_text(json.dumps({"targets": values}, ensure_ascii=False), encoding="utf-8")
    assert cli.load_targets(path) == values
    assert cli.load_targets(None) == []


@pytest.mark.parametrize("data", [
    [], {}, {"targets": {}}, {"targets": [], "account": "secret"},
    {"targets": [None]}, {"targets": [TARGET, TARGET]},
    {"targets": [{**TARGET, "send": True}]}, {"targets": [{**TARGET, "kind": "public"}]},
    {"targets": [{**TARGET, "alias": f"friend{i}"} for i in range(9)]},
])
def test_bad_target_shapes_fail_before_any_worker_is_started(tmp_path, data):
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((TypeError, ValueError)):
        cli.load_targets(path)


@pytest.mark.parametrize("raw", ["{broken SECRET json", " " * 16001])
def test_invalid_or_oversized_target_files_are_rejected(tmp_path, raw):
    path = tmp_path / "targets.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        cli.load_targets(path)


def test_worker_receives_targets_on_stdin_not_command_line_and_is_hidden_on_windows():
    calls = []

    def runner(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=json.dumps(SAFE), stderr="ignored SECRET stderr")

    assert cli.isolated_probe([TARGET], timeout=7, runner=runner) == SAFE
    command, options = calls[0]
    assert command[-3:] == ["-m", "companion_wechat", "_probe_worker"]
    assert "SENSITIVE" not in str(command)
    assert json.loads(options["input"]) == [TARGET]
    assert options["timeout"] == 7 and options["capture_output"] is True
    assert options["encoding"] == "utf-8" and options["check"] is False
    assert options["creationflags"] == (subprocess.CREATE_NO_WINDOW if cli.os.name == "nt" else 0)


def test_timeout_discards_captured_partial_output_and_does_not_retry():
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("secret command", 3, output="SECRET chat", stderr="SECRET account")

    report = cli.isolated_probe([TARGET], runner=runner)
    assert report["status"] == "probe_timeout"
    assert "SECRET" not in json.dumps(report) and len(calls) == 1


@pytest.mark.parametrize("returncode,stdout", [
    (1, "SECRET traceback"), (0, "SECRET invalid json"), (0, "x" * 64001),
    (0, json.dumps([])), (0, json.dumps({"read_only": True})),
    (0, json.dumps({**SAFE, "messages_sent": 1})), (0, json.dumps({**SAFE, "schema_version": True})),
    (0, json.dumps({**SAFE, "status": ["SECRET"]})), (0, json.dumps({**SAFE, "status": "SECRET"})),
    (0, json.dumps({**SAFE, "raw_exception": "SECRET"})), (0, json.dumps({**SAFE, "read_only": False})),
], ids=["nonzero", "invalid_json", "oversized", "array", "missing_fields", "messages_sent",
        "boolean_version", "list_status", "unknown_status", "unknown_field", "not_readonly"])
def test_worker_failure_or_invalid_protocol_is_sanitized(returncode, stdout):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="SECRET stderr")

    report = cli.isolated_probe([], runner=runner)
    assert report["status"] == "probe_worker_failed"
    assert "SECRET" not in json.dumps(report)


def test_process_start_error_is_sanitized():
    def runner(*args, **kwargs):
        raise OSError("SECRET path")

    assert cli.isolated_probe([], runner=runner)["status"] == "probe_worker_failed"


def test_main_writes_redacted_report_and_calls_probe_once(tmp_path, monkeypatch, capsys):
    path, output = tmp_path / "targets.json", tmp_path / "reports" / "probe.json"
    path.write_text(json.dumps({"targets": [TARGET]}), encoding="utf-8")
    calls = []

    def probe(targets, **kwargs):
        calls.append((targets, kwargs))
        return SAFE

    monkeypatch.setattr(cli, "isolated_probe", probe)
    assert cli.main(["probe", "--targets", str(path), "--output", str(output), "--timeout", "5"]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == SAFE
    assert json.loads(capsys.readouterr().out) == SAFE
    assert calls == [([TARGET], {"timeout": 5})]


@pytest.mark.parametrize("arguments", [["--timeout", "0"], ["--timeout", "61"], ["--timeout", "nan"],
                                     ["--timeout", "inf"], ["--targets", "SECRET_missing.json"]])
def test_main_rejects_invalid_configuration_without_calling_worker(arguments, monkeypatch, capsys):
    monkeypatch.setattr(cli, "isolated_probe", lambda *args, **kwargs: pytest.fail("worker must not start"))
    assert cli.main(["probe", *arguments]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "probe_configuration_error"
    assert "SECRET" not in output


def test_unready_environment_has_nonzero_cli_exit(monkeypatch, capsys):
    monkeypatch.setattr(cli, "isolated_probe", lambda *args, **kwargs: {**SAFE, "status": "login_required"})
    assert cli.main(["probe"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "login_required"


def test_worker_only_builds_target_specs_and_calls_read_only_probe(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps([TARGET])))
    seen = []

    def probe(targets):
        seen.extend(targets)
        return SAFE

    monkeypatch.setattr(cli, "run_probe", probe)
    assert cli.main(["_probe_worker"]) == 0
    assert seen[0].title == TARGET["title"]
    assert json.loads(capsys.readouterr().out) == SAFE


@pytest.mark.parametrize("input_text", ["SECRET bad json", "x" * 16001, "{}", "[null]"],
                         ids=["bad_json", "oversized", "object", "null_target"])
def test_worker_invalid_input_is_silent(input_text, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(input_text))
    monkeypatch.setattr(cli, "run_probe", lambda *args: pytest.fail("probe must not start"))
    assert cli.main(["_probe_worker"]) == 2
    assert capsys.readouterr().out == ""


def test_worker_runtime_exception_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("[]"))

    def fail(*args):
        raise RuntimeError("SECRET private UI data")

    monkeypatch.setattr(cli, "run_probe", fail)
    assert cli.main(["_probe_worker"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["send", "login", "start", "reply"])
def test_no_live_account_control_or_sending_commands_exist(command, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([command])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
