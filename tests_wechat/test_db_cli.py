"""CLI boundary tests with synthetic files and fake subprocesses only."""
import copy
import io
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from companion_wechat import db_cli as cli
from companion_wechat import db_probe


def report(status="ready"):
    value = cli.failure(status)
    value.update(client_version="4.1.15.13", prefix="XLD-1234ABCD", polls=0,
                 account_candidates=3, account_matches=1, message_sources=2,
                 minimized_samples=0, foreground_target_samples=0,
                 elapsed_seconds=0.0, max_poll_seconds=0.0, cpu_seconds=0.0,
                 targets={alias: {"new_rows": 0, "marker_counts": [0, 0, 0],
                                  "minimized_marker_counts": [0, 0, 0],
                                  "unverified_sender_markers": 0, "unstable_id_markers": 0}
                          for alias in ("owner_main", "family")})
    return value


class RecordingInput(io.StringIO):
    def close(self):
        if not self.closed:
            self.recorded = self.getvalue()
        super().close()


class FakeChild:
    def __init__(self, lines=(), *, running=False, returncode=0, wait_error=False, kill_error=False):
        self.stdin = RecordingInput()
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.running = running
        self.returncode = None if running else returncode
        self.exit_value = returncode
        self.wait_error = wait_error
        self.kill_error = kill_error
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        return None if self.running else self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_error:
            raise subprocess.TimeoutExpired("PRIVATE-WORKER-PATH", timeout)
        self.running = False
        self.returncode = self.exit_value
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        if self.kill_error:
            raise OSError("PRIVATE-SYSTEM-ERROR")
        self.running = False
        self.returncode = self.exit_value


def install_child(monkeypatch, child):
    launches = []

    def popen(command, **kwargs):
        launches.append((command, kwargs))
        return child

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    return launches


@pytest.fixture
def configs(tmp_path):
    account = tmp_path / "synthetic-account"
    account.mkdir()
    locations, targets = tmp_path / "locations.json", tmp_path / "targets.json"
    locations.write_text(json.dumps({"account_dirs": [str(account)]}), encoding="utf-8")
    targets.write_text(json.dumps({"automatic_sending_enabled": False,
                                  "private": {"wechat_id_hint": "PRIVATE-OWNER-HINT"},
                                  "group": {"window_title_hint": "PRIVATE-GROUP-HINT"}}), encoding="utf-8")
    return account, locations, targets


def main_args(configs):
    return ["--locations", str(configs[1]), "--targets", str(configs[2]), "--prefix", "XLD-1234ABCD"]


def test_complete_report_is_accepted_without_mutating_it():
    value = report()
    original = copy.deepcopy(value)
    assert cli.validate_report(value) == original
    assert value == original


@pytest.mark.parametrize("status", ["ready", "observing", "watch_complete"])
@pytest.mark.parametrize("missing", ["client_version", "prefix", "targets", "account_candidates",
                                    "account_matches", "message_sources", "polls", "minimized_samples",
                                    "foreground_target_samples", "elapsed_seconds", "max_poll_seconds",
                                    "cpu_seconds"])
def test_success_states_require_complete_evidence(status, missing):
    value = report(status)
    del value[missing]
    with pytest.raises(ValueError, match="invalid_report"):
        cli.validate_report(value)


@pytest.mark.parametrize("counter", ["account_candidates", "account_matches", "message_sources", "polls",
                                    "minimized_samples", "foreground_target_samples"])
def test_discrete_counters_cannot_be_float(counter):
    value = report()
    value[counter] = 1.0
    with pytest.raises(ValueError, match="invalid_report"):
        cli.validate_report(value)


@pytest.mark.parametrize("mutation", [
    "top_secret", "nested_secret", "extra_target", "missing_target", "missing_counter",
    "private_prefix", "unknown_status", "bad_client_version", "missing_safety_field",
    "bool_schema", "false_readonly", "integer_keys_flag", "content_exported", "messages_sent",
])
def test_report_allowlist_rejects_unknown_or_private_fields_at_every_depth(mutation):
    value = report()
    if mutation == "top_secret":
        value["secret"] = "PRIVATE-MESSAGE"
    elif mutation == "nested_secret":
        value["targets"]["family"]["content"] = "PRIVATE-MESSAGE"
    elif mutation == "extra_target":
        value["targets"]["PRIVATE-PERSON"] = value["targets"]["family"]
    elif mutation == "missing_target":
        del value["targets"]["family"]
    elif mutation == "missing_counter":
        del value["targets"]["family"]["new_rows"]
    elif mutation == "private_prefix":
        value["prefix"] = "PRIVATE-IDENTIFIER"
    elif mutation == "unknown_status":
        value["status"] = "PRIVATE-ERROR"
    elif mutation == "bad_client_version":
        value["client_version"] = "PRIVATE-PATH"
    elif mutation == "missing_safety_field":
        del value["keys_exported"]
    elif mutation == "bool_schema":
        value["schema_version"] = True
    elif mutation == "false_readonly":
        value["read_only"] = False
    elif mutation == "integer_keys_flag":
        value["keys_exported"] = 0
    elif mutation == "content_exported":
        value["message_content_exported"] = True
    else:
        value["messages_sent"] = 1
    with pytest.raises(ValueError, match="invalid_report"):
        cli.validate_report(value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1, 100001, True, "PRIVATE", {}])
def test_report_counters_reject_nan_infinity_booleans_and_private_values(bad):
    value = report()
    value["elapsed_seconds"] = bad
    with pytest.raises(ValueError, match="invalid_report"):
        cli.validate_report(value)


@pytest.mark.parametrize("bad", [[1, 2], [0, 0, 0, 0], [0, True, 0], [0, 0.1, 0],
                                 [0, -1, 0], [0, 10001, 0], {"PRIVATE": 1}, "PRIVATE"])
def test_marker_counts_have_bounded_integer_shape(bad):
    value = report()
    value["targets"]["family"]["marker_counts"] = bad
    with pytest.raises(ValueError, match="invalid_report"):
        cli.validate_report(value)


def test_load_config_only_transfers_two_hints_and_account_paths(configs):
    account, locations, targets = configs
    assert cli.load_config(locations, targets) == {"account_dirs": [str(account)],
                                                  "private_hint": "PRIVATE-OWNER-HINT",
                                                  "group_hint": "PRIVATE-GROUP-HINT"}


def test_locations_can_pin_one_pid_without_changing_account_or_targets(configs):
    account, locations, targets = configs
    locations.write_text(json.dumps({"account_dirs": [str(account)], "client_pid": 77}), encoding="utf-8")
    assert cli.load_config(locations, targets) == {
        "account_dirs": [str(account)], "client_pid": 77,
        "private_hint": "PRIVATE-OWNER-HINT", "group_hint": "PRIVATE-GROUP-HINT",
    }


@pytest.mark.parametrize("pid", [None, True, "77", 0, -1, 2**32, 77.0, {}, []])
def test_locations_invalid_pid_pin_is_rejected(configs, pid):
    account, locations, targets = configs
    locations.write_text(json.dumps({"account_dirs": [str(account)], "client_pid": pid}), encoding="utf-8")
    with pytest.raises(db_probe.ProbeError, match="configuration_invalid"):
        cli.load_config(locations, targets)


def test_locations_do_not_accept_unknown_selector_or_fallback_fields(configs):
    account, locations, targets = configs
    locations.write_text(json.dumps({"account_dirs": [str(account)], "client_pid": 77,
                                    "fallback_pid": 88}), encoding="utf-8")
    with pytest.raises(db_probe.ProbeError, match="configuration_invalid"):
        cli.load_config(locations, targets)


@pytest.mark.parametrize("enabled", [True, 0, None, "false"])
def test_auto_sending_must_be_exactly_disabled(configs, enabled):
    _, locations, targets = configs
    data = json.loads(targets.read_text(encoding="utf-8"))
    data["automatic_sending_enabled"] = enabled
    targets.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(db_probe.ProbeError, match="configuration_invalid"):
        cli.load_config(locations, targets)


def test_config_file_read_is_bounded_even_if_it_grows_after_stat():
    class GrowingPath:
        def stat(self):
            return SimpleNamespace(st_size=1)

        def read_text(self, **_):
            pytest.fail("unbounded read_text after a racy stat")

        def open(self, *_, **__):
            return io.StringIO("x" * 16001)

    with pytest.raises((db_probe.ProbeError, ValueError)):
        cli.load_json(GrowingPath())


def test_large_config_is_rejected_before_json_decode(tmp_path):
    path = tmp_path / "oversize.json"
    path.write_text(" " * 16001, encoding="utf-8")
    with pytest.raises((db_probe.ProbeError, ValueError)):
        cli.load_json(path)


@pytest.mark.parametrize("destination", ["account", "locations", "targets", "wrong_suffix"])
def test_output_cannot_replace_source_directory_or_config(configs, tmp_path, monkeypatch, capsys, destination):
    account, locations, targets = configs
    output = {"account": account / "report.json", "locations": locations,
              "targets": targets, "wrong_suffix": tmp_path / "report.db"}[destination]
    monkeypatch.setattr(cli, "isolated", lambda *_a, **_k: pytest.fail("worker must not start"))
    assert cli.main(main_args(configs) + ["--output", str(output)]) == 2
    printed = capsys.readouterr().out
    assert json.loads(printed)["status"] == "configuration_invalid"
    assert "PRIVATE" not in printed and str(account) not in printed


@pytest.mark.parametrize("options", [["--interval", "nan"], ["--interval", "inf"],
                                     ["--interval", "0.49"], ["--interval", "5.1"],
                                     ["--duration", "-1"], ["--duration", "901"],
                                     ["--prefix", "PRIVATE"]])
def test_cli_numeric_and_marker_validation_precede_worker(configs, monkeypatch, capsys, options):
    monkeypatch.setattr(cli, "isolated", lambda *_a, **_k: pytest.fail("worker must not start"))
    assert cli.main(main_args(configs) + options) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_invalid"


def test_output_uses_exclusive_temp_file_without_following_existing_hardlink(configs, tmp_path, monkeypatch):
    source = configs[0] / "synthetic-source.db"
    source.write_bytes(b"UNMODIFIED-SOURCE")
    output = tmp_path / "report.json"
    old_temp = output.with_suffix(".json.tmp")
    os.link(source, old_temp)

    def isolated(_config, **kwargs):
        value = report()
        kwargs["emit"](value)
        return value

    monkeypatch.setattr(cli, "isolated", isolated)
    assert cli.main(main_args(configs) + ["--output", str(output)]) == 0
    assert source.read_bytes() == b"UNMODIFIED-SOURCE"
    assert old_temp.read_bytes() == b"UNMODIFIED-SOURCE"
    assert cli.validate_report(json.loads(output.read_text(encoding="utf-8")))["status"] == "ready"


def test_isolated_uses_new_python_process_and_keeps_private_config_off_command_line(monkeypatch):
    child = FakeChild([json.dumps(report("ready")), json.dumps(report("watch_complete"))])
    launches = install_child(monkeypatch, child)
    emissions = []
    config = {"private_hint": "PRIVATE-OWNER", "group_hint": "PRIVATE-GROUP", "account_dirs": ["PRIVATE-PATH"]}
    result = cli.isolated(config, emit=emissions.append)
    command, options = launches[0]
    assert command == [cli.sys.executable, "-m", "companion_wechat.db_cli", "_worker"]
    assert "PRIVATE" not in repr(command)
    assert options["stderr"] == subprocess.DEVNULL
    assert options["stdin"] == options["stdout"] == subprocess.PIPE
    assert json.loads(child.stdin.recorded)["config"] == config
    assert result["status"] == "watch_complete"
    assert [r["status"] for r in emissions] == ["ready", "watch_complete"]
    assert child.stdout.closed


@pytest.mark.parametrize("line", ["PRIVATE-RAW-LOG", '{"status":"PRIVATE"}',
                                  json.dumps({**report(), "private": "SECRET"}), "x" * 64001],
                         ids=["raw-log", "unknown-status", "private-json", "oversized-line"])
def test_worker_output_is_dropped_until_validated_and_never_printed(monkeypatch, capsys, line):
    child = FakeChild([line])
    install_child(monkeypatch, child)
    emissions = []
    result = cli.isolated({}, emit=emissions.append)
    assert result["status"] == "worker_failed"
    assert emissions == []
    assert capsys.readouterr() == ("", "")
    assert "PRIVATE" not in json.dumps(result)


def test_nonzero_worker_exit_does_not_promote_ready_report_to_success(monkeypatch):
    child = FakeChild([json.dumps(report())], returncode=7)
    install_child(monkeypatch, child)
    assert cli.isolated({})["status"] == "worker_failed"


def install_expiring_clock(monkeypatch):
    calls = []

    def clock():
        calls.append(None)
        return 0.0 if len(calls) <= 2 else 200.0

    monkeypatch.setattr(cli.time, "monotonic", clock)


def test_worker_startup_deadline_kills_child_and_closes_pipes(monkeypatch):
    child = FakeChild(running=True)
    install_child(monkeypatch, child)
    install_expiring_clock(monkeypatch)
    assert cli.isolated({}, startup_timeout=1)["status"] == "probe_timeout"
    assert child.kill_calls == 1
    assert child.stdout.closed and child.stdin.closed


@pytest.mark.parametrize("fault", ["wait", "kill"])
def test_cleanup_failures_cannot_escape_or_replace_redacted_report(monkeypatch, capsys, fault):
    child = FakeChild(running=True, wait_error=fault == "wait", kill_error=fault == "kill")
    install_child(monkeypatch, child)
    install_expiring_clock(monkeypatch)
    result = cli.isolated({}, startup_timeout=1)
    if fault == "kill":
        assert result["status"] == "worker_cleanup_failed"
    else:
        assert result["status"] in {"probe_timeout", "worker_failed"}
    assert "PRIVATE" not in json.dumps(result)
    assert capsys.readouterr() == ("", "")
    # Closing a live child's buffered pipe synchronously can itself block.
    if not child.running:
        assert child.stdout.closed


def test_blocked_stdin_is_covered_by_parent_deadline(monkeypatch):
    release, finished = threading.Event(), threading.Event()

    class BlockingInput(RecordingInput):
        def write(self, value):
            release.wait(timeout=1)
            finished.set()
            return super().write(value)

    child = FakeChild(running=True)
    child.stdin = BlockingInput()
    child.killed_before_write_finished = False
    original = child.kill

    def kill():
        child.killed_before_write_finished = not finished.is_set()
        release.set()
        original()

    child.kill = kill
    install_child(monkeypatch, child)
    install_expiring_clock(monkeypatch)
    try:
        result = cli.isolated({"payload": "x" * 12000}, startup_timeout=1)
    finally:
        release.set()
    assert result["status"] == "probe_timeout"
    assert child.killed_before_write_finished


def test_worker_protocol_invalid_input_cannot_call_probe(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('{"PRIVATE":"SECRET"}'))
    monkeypatch.setattr(db_probe, "run", lambda **_: pytest.fail("probe must not run"))
    assert cli.main(["_worker"]) == 0
    value = capsys.readouterr().out
    assert json.loads(value)["status"] == "configuration_invalid"
    assert "PRIVATE" not in value and "SECRET" not in value


def test_worker_probe_exception_is_replaced_with_fixed_status(monkeypatch, capsys):
    payload = {"config": {}, "duration": 0, "interval": 1, "prefix": "XLD-1234ABCD"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))

    def fail(**_):
        raise RuntimeError("PRIVATE-KEY-AND-PATH")

    monkeypatch.setattr(db_probe, "run", fail)
    assert cli.main(["_worker"]) == 0
    value = capsys.readouterr().out
    assert json.loads(value)["status"] == "probe_failed"
    assert "PRIVATE" not in value


def test_worker_rejects_private_field_from_probe_before_stdout(monkeypatch, capsys):
    payload = {"config": {}, "duration": 0, "interval": 1, "prefix": "XLD-1234ABCD"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))

    def run(**kwargs):
        kwargs["emit"]({**report(), "plaintext": "PRIVATE-CONTENT"})

    monkeypatch.setattr(db_probe, "run", run)
    assert cli.main(["_worker"]) == 0
    value = capsys.readouterr().out
    assert json.loads(value)["status"] == "probe_failed"
    assert "PRIVATE" not in value
