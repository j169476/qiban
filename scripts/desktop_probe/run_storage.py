"""Verify encrypted storage and dependency order using a new database and vault entry."""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from credential_vault import VaultCredentials
from run_lifecycle import api
from run_postgres import Host, prepare, wait_state
from storage_worker import VAULT

from companion_core.postgres import PostgresStore
from companion_core.process_scope import ProcessScope


class StackHost:
    def __init__(self, exe, stack, evidence, pg, storage_logs, *, failure=None, env=None, cwd=None, deadline_seconds=None):
        evidence.mkdir()
        self.scope, self.directory, self.handles = ProcessScope(), evidence, []
        try:
            env = dict(env or os.environ)
            env.pop("CODEX_THREAD_ID", None)
            if deadline_seconds is not None:
                if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 3600:
                    raise ValueError("Invalid stack test host deadline")
                env["QIBAN_TEST_STACK_TIMEOUT_SECONDS"] = str(deadline_seconds)
            self.process = subprocess.Popen([str(exe), "--stack-host", str(stack), str(evidence)], env=env, cwd=cwd,
                stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            self.scope.attach(self.process)
            self.scope.resume()
            if failure:
                assert self.process.wait(timeout=60) == 1
                state = json.loads((evidence / "state.json").read_text(encoding="utf8"))
                assert state["state"] == "failed" and state["detail"] == failure, state
                assert not (Path(pg["DataDirectory"]) / "postmaster.pid").exists()
            else:
                wait_state(evidence, self.process, "ready")
                if deadline_seconds is not None:
                    state = json.loads((evidence / "state.json").read_text(encoding="utf8"))
                    if state.get("deadline_seconds") != deadline_seconds:
                        self.stop()
                        raise RuntimeError("Rebuild the desktop test host: requested deadline was not acknowledged")
                database = json.loads((Path(pg["LogDirectory"]) / "database-process.json").read_text(encoding="utf8"))
                worker = json.loads((storage_logs / "storage-process.json").read_text(encoding="utf8"))
                self.python_pid = worker["pid"]
                for pid in [database["pid"], worker["pid"]]:
                    handle = api.OpenProcess(0x100000, False, pid)
                    if not handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    self.handles.append(handle)
        except BaseException:
            self.close()
            raise

    def stop(self):
        (self.directory / "stop").write_text("stop", encoding="utf8")
        assert self.process.wait(timeout=30) == 0
        assert all(api.WaitForSingleObject(handle, 5000) == 0 for handle in self.handles)

    def close(self):
        self.scope.close()
        for handle in self.handles:
            api.CloseHandle(handle)
        self.handles = []


def ciphertext(options):
    with psycopg.connect(**options, autocommit=True) as db:
        rows = db.execute("SELECT kind,id,body FROM content_records ORDER BY kind,id").fetchall()
    assert rows and all(bytes(row[2]).startswith(b"CC1") for row in rows)
    assert all(b"project_id" not in bytes(row[2]) for row in rows)
    return hashlib.sha256(b"".join(bytes(row[2]) for row in rows)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    exe, evidence = args.exe.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    application = evidence / "搬移后的应用"
    shutil.copytree(exe.parent, application)
    exe = application / exe.name
    bundle = application / "resources" / "storage"
    shutil.copytree(args.worker.resolve(strict=True).parent, bundle)
    worker = bundle / args.worker.name
    unrelated = evidence / "unrelated-cwd"
    unrelated.mkdir()
    # Any external source import would fail; frozen runtime must use its own library paths.
    (unrelated / "companion_core.py").write_text('raise RuntimeError("external source loaded")', encoding="utf8")
    isolated = {**os.environ, "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
                "PYTHONHOME": str(unrelated / "no-python"), "PYTHONPATH": str(unrelated),
                "PYTHONNOUSERSITE": "1"}
    before_files = {p.relative_to(application).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in application.rglob("*") if p.is_file()}
    vault, installation = VaultCredentials(), uuid.uuid4().hex
    assert vault.get_password(VAULT, installation) is None
    report, hosts = {"passed": False, "cases": [], "runtime": str(worker)}, []
    def start(name, *, failure=None):
        result = StackHost(exe, stack, evidence / name, pg, logs, failure=failure, env=isolated, cwd=unrelated)
        hosts.append(result)
        return result
    try:
        result = subprocess.run([str(worker), "--check-resources"], env=isolated, cwd=unrelated,
                                capture_output=True, text=True, check=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        resources = json.loads(result.stdout)
        assert resources["frozen"] and resources["postgres_driver"] == "binary", resources
        assert Path(resources["root"]) == bundle / "_internal", resources
        assert all(Path(p).is_relative_to(bundle) for p in resources["search_paths"]), resources
        report["resources"] = resources
        report["cases"].append("relocated_frozen_runtime_without_developer_python_or_source")
        pg, pg_path, options = prepare(args.bin.resolve(strict=True), evidence)
        report["test_runtime"] = str(Path(pg["DataDirectory"]).parent)
        database = Host(exe, pg_path, evidence / "provision")
        hosts.append(database)
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute("CREATE DATABASE companion_test")
        options = {**options, "dbname": "companion_test"}
        key = secrets.token_bytes(32)
        store_root = evidence / "encrypted-data"
        store = PostgresStore(store_root, options, key)
        try:
            store.migrate()
            assert store.initialize()["ok"]
            before = ciphertext(options)
        finally:
            store.database.close()
        database.stop()
        database.close()
        credential = json.dumps({"data_key": base64.b64encode(key).decode(), "database_password": options["password"]})
        vault.set_password(VAULT, installation, credential)
        assert vault.get_password(VAULT, installation) == credential
        config = evidence / "storage-config.json"
        config.write_text(json.dumps({"installation": installation, "data": str(store_root),
            "database": {k: options[k] for k in ["host", "port", "user", "dbname"]}}), encoding="utf8")
        logs = evidence / "storage-logs"
        spec = evidence / "storage-spec.json"
        spec.write_text(json.dumps({"WorkerExecutable": str(worker),
            "ConfigurationPath": str(config), "LogDirectory": str(logs)}), encoding="utf8")
        stack = evidence / "stack-spec.json"
        stack.write_text(json.dumps({"PostgresSpecPath": str(pg_path), "StorageSpecPath": str(spec)}), encoding="utf8")
        host = start("normal")
        assert ciphertext(options) == before
        host.stop()
        transitions = [json.loads(line) for line in (host.directory / "events.jsonl").read_text(encoding="utf8").splitlines()]
        states = [[m["state"] for m in row["members"]] for row in transitions]
        assert ["ready", "starting"] in states and ["ready", "stopped"] in states
        assert states.index(["ready", "stopping"]) < states.index(["stopping", "stopped"])
        host.close()
        report["cases"].append("encrypted_store_ready_and_reverse_stop")
        vault.delete_password(VAULT, installation)
        start("missing-vault", failure="storage_vault_missing")
        report["cases"].append("missing_credentials_stop_database_without_recreation")
        assert vault.get_password(VAULT, installation) is None
        wrong = json.dumps({"data_key": base64.b64encode(secrets.token_bytes(32)).decode(), "database_password": options["password"]})
        vault.set_password(VAULT, installation, wrong)
        start("wrong-key", failure="storage_integrity")
        assert vault.get_password(VAULT, installation) == wrong
        report["cases"].append("wrong_key_rejected_without_reset")
        vault.set_password(VAULT, installation, credential)
        host = start("reopen")
        assert ciphertext(options) == before
        host.process.kill()
        host.process.wait(timeout=10)
        assert all(api.WaitForSingleObject(handle, 10000) == 0 for handle in host.handles)
        host.close()
        report["cases"].append("vault_reopen_and_host_crash_cleanup")
        host = start("recovery")
        assert ciphertext(options) == before
        host.stop()
        host.close()
        report["cases"].append("encrypted_content_unchanged_after_recovery")
        # Deliberately damage only this copied bundle, restoring bytes in every case.
        targets = [("missing-worker", worker, "storage_runtime_missing", None),
                   ("missing-python", bundle / "_internal/python311.dll", "storage_bundle_invalid", None),
                   ("changed-schema", bundle / "_internal/companion_core/schemas.json", "storage_bundle_invalid", b"{}")]
        for name, target, code, replacement in targets:
            saved = target.read_bytes()
            try:
                if replacement is None:
                    target.unlink()
                else:
                    target.write_bytes(replacement)
                start(name, failure=code)
                assert vault.get_password(VAULT, installation) == credential
                report["cases"].append(name+"_rejected_without_reset")
                if name == "changed-schema":
                    profile = evidence / "window-damaged-bundle"
                    profile.mkdir()
                    scope = ProcessScope()
                    try:
                        window = subprocess.Popen([str(exe), "--database-failure-smoke", "--stack-spec", str(stack),
                            "--data-dir", str(profile)], env=isolated, cwd=unrelated, stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
                        scope.attach(window)
                        scope.resume()
                        assert window.wait(timeout=90) == 0
                        assert json.loads((profile / "result.json").read_text(encoding="utf8"))["passed"]
                        assert not (Path(pg["DataDirectory"]) / "postmaster.pid").exists()
                        report["cases"].append("damaged_bundle_window_remains_usable")
                    finally:
                        scope.close()
            finally:
                target.write_bytes(saved)
        for name, flag in [("window", "--smoke"), ("window-missing-vault", "--database-failure-smoke")]:
            if "missing" in name:
                vault.delete_password(VAULT, installation)
            profile = evidence / name
            profile.mkdir()
            scope = ProcessScope()
            try:
                window = subprocess.Popen([str(exe), flag, "--stack-spec", str(stack), "--data-dir", str(profile)],
                    stdin=subprocess.DEVNULL, env=isolated, cwd=unrelated, creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
                scope.attach(window)
                scope.resume()
                assert window.wait(timeout=90) == 0
                result = json.loads((profile / "result.json").read_text(encoding="utf8"))
                assert result["passed"]
                assert not (Path(pg["DataDirectory"]) / "postmaster.pid").exists()
                report["cases"].append(name+"_passed")
            finally:
                scope.close()
        after_files = {p.relative_to(application).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in application.rglob("*") if p.is_file()}
        assert after_files == before_files, "Application files changed during execution"
        report["cases"].append("application_directory_unchanged")
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            for host in hosts:
                host.close()
        finally:
            try:
                if vault.get_password(VAULT, installation) is not None:
                    vault.delete_password(VAULT, installation)
                report["test_vault_removed"] = vault.get_password(VAULT, installation) is None
            finally:
                (evidence / "storage-result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
