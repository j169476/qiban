"""Exercise the desktop PostgreSQL adapter against a newly initialized test cluster."""
from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from run_lifecycle import api

from companion_core.process_scope import ProcessScope


def wait_state(directory, process, target):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            state = json.loads((directory / "state.json").read_text(encoding="utf8"))
            if state["state"] == target:
                return state
            if state["state"] == "failed":
                raise RuntimeError(f"Database component failed: {state['detail']}")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if process.poll() is not None:
            raise RuntimeError(f"Database host exited: {process.returncode}")
        time.sleep(.05)
    raise TimeoutError("Database component state deadline")


class Host:
    def __init__(self, exe, spec, evidence, *, failure=False, deadline_seconds=60):
        if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 3600:
            raise ValueError('Test host deadline must be 1 to 3600 seconds')
        evidence.mkdir()
        self.directory, self.scope, self.handle = evidence, ProcessScope(), None
        try:
            command = [str(exe), "--postgres-host", str(spec), str(evidence)]
            if deadline_seconds != 60:
                command.append(str(deadline_seconds))
            self.process = subprocess.Popen(command,
                stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            self.scope.attach(self.process)
            self.scope.resume()
            if failure:
                assert self.process.wait(timeout=40) == 1
                state = json.loads((evidence / "state.json").read_text(encoding="utf8"))
                assert state["state"] == "failed"
                self.failure = state
            else:
                state = wait_state(evidence, self.process, "ready")
                config = json.loads(spec.read_text(encoding="utf8"))
                server = json.loads((Path(config["LogDirectory"]) / "database-process.json").read_text(encoding="utf8"))
                assert server["owned"] and server["worker"] == state["worker"]
                self.handle = api.OpenProcess(0x100000, False, server["pid"])
                if not self.handle:
                    raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def stop(self):
        (self.directory / "stop").write_text("stop", encoding="utf8")
        assert self.process.wait(timeout=25) == 0
        state = json.loads((self.directory / "state.json").read_text(encoding="utf8"))
        assert state["state"] == "stopped" and state["detail"] == "graceful_stop"
        assert api.WaitForSingleObject(self.handle, 5000) == 0

    def crash(self):
        self.process.kill()  # Only the host; assert cleanup before closing the test scope.
        self.process.wait(timeout=10)
        assert api.WaitForSingleObject(self.handle, 10000) == 0

    def close(self):
        self.scope.close()
        if self.handle:
            api.CloseHandle(self.handle)
            self.handle = None


def prepare(binaries, evidence):
    runtime = Path(tempfile.mkdtemp(prefix="qiban-desktop-pg-"))
    assert str(runtime).isascii(), "This probe requires an ASCII PostgreSQL runtime path"
    subprocess.run(["icacls.exe", str(runtime), "/inheritance:r", "/grant:r",
                    getpass.getuser()+":(OI)(CI)F", "SYSTEM:(OI)(CI)F"],
                   check=True, stdout=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    bundle = runtime / "pgsql"
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    # Keep bin/lib/share together; no copying or modifying the source distribution.
    subprocess.run(["powershell.exe", "-NoProfile", "-Command",
        f"New-Item -ItemType Junction -Path {quote(bundle)} -Value {quote(binaries.parent)} | Out-Null"],
        check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    binaries = bundle / "bin"
    log = runtime / "logs"
    log.mkdir()
    password = secrets.token_urlsafe(36)
    pw = runtime / "init-password"
    pw.write_text(password, encoding="ascii")
    try:
        with (log / "init.log").open("wb") as output:
            subprocess.run([str(binaries / "initdb.exe"), "-D", str(runtime / "data"), "-U", "qiban_probe",
                "--encoding=UTF8", "--locale=C", "--auth=scram-sha-256", "--pwfile="+str(pw)],
                check=True, stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        pw.unlink(missing_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (runtime / "data/postgresql.conf").open("a", encoding="utf8") as config:
        config.write(f"\nlisten_addresses='127.0.0.1'\nport={port}\nlog_statement='none'\nlog_min_error_statement='panic'\n")
    token = secrets.token_hex(24)
    (runtime / "data/qiban-probe-owner").write_text(token, encoding="ascii")
    spec = {"BinDirectory": str(binaries), "DataDirectory": str(runtime / "data"),
            "LogDirectory": str(log), "Port": port, "OwnerToken": token,
            "MajorVersion": (runtime / "data/PG_VERSION").read_text().strip()}
    path = evidence / "postgres-spec.json"
    path.write_text(json.dumps(spec), encoding="utf8")
    options = {"host": "127.0.0.1", "port": port, "user": "qiban_probe", "password": password,
               "dbname": "postgres", "connect_timeout": 5}
    return spec, path, options


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--bin", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    exe, evidence = args.exe.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "cases": []}
    hosts = []
    try:
        spec, spec_path, options = prepare(args.bin.resolve(strict=True), evidence)
        report["test_runtime"] = str(Path(spec["DataDirectory"]).parent)
        host = Host(exe, spec_path, evidence / "normal")
        hosts.append(host)
        with psycopg.connect(**options, autocommit=True) as db:
            assert Path(db.execute("SHOW data_directory").fetchone()[0]) == Path(spec["DataDirectory"])
            report["server_version"] = db.execute("SHOW server_version").fetchone()[0]
            db.execute("CREATE TABLE qiban_probe (id integer PRIMARY KEY, content text)")
            db.execute("INSERT INTO qiban_probe VALUES (1, 'committed')")
            duplicate = Host(exe, spec_path, evidence / "duplicate", failure=True)
            hosts.append(duplicate)
            assert db.execute("SELECT count(*) FROM qiban_probe").fetchone()[0] == 1
            report["cases"].append("existing_instance_not_taken_over")
        pending = psycopg.connect(**options)
        try:
            pending.execute("INSERT INTO qiban_probe VALUES (2, 'uncommitted')")
            host.stop()
        finally:
            pending.close()
            host.close()
        report["cases"].append("graceful_stop_server_exited")
        host = Host(exe, spec_path, evidence / "restart")
        hosts.append(host)
        with psycopg.connect(**options, autocommit=True) as db:
            assert db.execute("SELECT id, content FROM qiban_probe ORDER BY id").fetchall() == [(1, "committed")]
        report["cases"].append("committed_preserved_uncommitted_rolled_back")
        host.crash()
        host.close()
        report["cases"].append("host_death_cleans_database_before_test_cleanup")
        host = Host(exe, spec_path, evidence / "recovery")
        hosts.append(host)
        with psycopg.connect(**options, autocommit=True) as db:
            assert db.execute("SELECT id, content FROM qiban_probe").fetchall() == [(1, "committed")]
            db.execute("INSERT INTO qiban_probe VALUES (3, 'after recovery')")
        host.stop()
        host.close()
        report["cases"].append("crash_recovery_read_and_write")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", spec["Port"]))
            listener.listen()
            blocked = Host(exe, spec_path, evidence / "port-conflict", failure=True)
            hosts.append(blocked)
            assert listener.getsockname()[1] == spec["Port"]
            events = [json.loads(line) for line in (Path(spec["LogDirectory"]) / "database-events.jsonl").read_text(encoding="utf8").splitlines()]
            assert events[-1]["detail"]["Message"] == "database_port_in_use"
            report["cases"].append("port_conflict_rejected")
        bad = evidence / "wrong-version.json"
        bad.write_text(json.dumps({**spec, "MajorVersion": "0"}), encoding="utf8")
        blocked = Host(exe, bad, evidence / "version-mismatch", failure=True)
        hosts.append(blocked)
        assert blocked.failure["detail"] == "database_version_mismatch"
        report["cases"].append("data_version_mismatch_rejected")
        for name, content, expected in [("malformed", "{", "database_spec_invalid"),
                ("missing-fields", "{}", "database_spec_invalid"),
                ("wrong-owner", json.dumps({**spec, "OwnerToken": "x"*48}), "database_owner_mismatch")]:
            invalid = evidence / (name+".json")
            invalid.write_text(content, encoding="utf8")
            blocked = Host(exe, invalid, evidence / name, failure=True)
            hosts.append(blocked)
            assert blocked.failure["detail"] == expected
            report["cases"].append(name+"_rejected")
        for name, config, flag in [("window", spec_path, "--smoke"),
                                    ("window-failure", bad, "--database-failure-smoke")]:
            profile = evidence / name
            profile.mkdir()
            scope = ProcessScope()
            try:
                window = subprocess.Popen([str(exe), flag, "--postgres-spec", str(config), "--data-dir", str(profile)],
                    stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
                scope.attach(window)
                scope.resume()
                assert window.wait(timeout=90) == 0
                result = json.loads((profile / "result.json").read_text(encoding="utf8"))
                assert result["passed"]
                checks = [e["name"] for e in result["events"] if e["name"].startswith("check_")]
                assert len(checks) == (9 if name == "window" else 3)
                logs = [json.loads(line) for line in (profile / "logs/events.jsonl").read_text(encoding="utf8").splitlines()]
                assert logs[-1]["name"] == "shutdown_completed"
                if name == "window":
                    assert logs[-1]["detail"]["detail"] == "graceful_stop"
                assert not (Path(spec["DataDirectory"]) / "postmaster.pid").exists()
                report["cases"].append(name+"_passed")
            finally:
                scope.close()
        host = Host(exe, spec_path, evidence / "final-data-check")
        hosts.append(host)
        with psycopg.connect(**options, autocommit=True) as db:
            assert db.execute("SELECT id, content FROM qiban_probe ORDER BY id").fetchall() == [(1, "committed"), (3, "after recovery")]
        host.stop()
        host.close()
        report["cases"].append("data_preserved_after_window_checks")
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for host in hosts:
            host.close()
        (evidence / "postgres-result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
