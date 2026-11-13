"""Real EXE account selection with two synthetic credentials and no network calls."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))

from credential_vault import VaultCredentials
from run_installation import InstallHost
from run_management import ProcessScope
from run_storage import StackHost
from storage_worker import VAULT, open_store

from qiban.desktop_siwc import KEY
from qiban.desktop_siwc_accounts import registration_id


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--resume-handoff", action="store_true")
    args = parser.parse_args()
    exe, evidence = args.exe.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    profile = evidence / "双账号合成资料"
    root = profile / "data/installation"
    report = {"passed": False, "model_calls_executed": False, "real_account_used": False}
    installer = host = None
    scope = ProcessScope()
    try:
        installer = InstallHost(exe, exe.parent, root, evidence / "install", os.environ)
        installer.wait()
        installer.close()
        installer = None
        host = StackHost(
            exe, root / "stack-spec.json", evidence / "seed", read(root / "postgres-spec.json"), root / "logs"
        )
        store = open_store(root / "storage-config.json")
        try:
            accounts = {}
            for number in ((1,) if args.resume_handoff else (1, 2)):
                value = {
                    "host": "synthetic-host",
                    "client_id": f"synthetic-client-{number}",
                    "subject": f"synthetic-subject-{number}",
                    "email": "synthetic@example.invalid",
                    "number": number,
                    "session": {
                        "access_token": f"SYNTHETIC-ACCESS-{number}",
                        "refresh_token": f"SYNTHETIC-REFRESH-{number}",
                        "scopes": ["chatgpt.tokens.use.direct"],
                        "expires_at": time.time() + 3600,
                    },
                }
                accounts[registration_id(value)] = value
            store.set_state(
                KEY,
                {
                    "format": 2,
                    "revision": 1,
                    "host": "synthetic-host",
                    "active": next(iter(accounts)),
                    "accounts": accounts,
                },
            )
            if args.resume_handoff:
                from qiban.desktop_runtime import SESSION

                store.set_state(SESSION, {**store.state(SESSION),
                    'account_switch':{'status':'stopping', 'resume':{}}})
        finally:
            store.database.close()
        host.stop()
        host.close()
        host = None
        with (evidence / "window.log").open("wb") as log:
            child = subprocess.Popen(
                [str(exe), "--data-dir", str(profile), "--account-picker-smoke"],
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x4,
            )
            scope.attach(child)
            scope.resume()
            assert child.wait(timeout=150) == 0, "Account picker window failed"
        result = read(profile / "result.json")
        assert result["passed"]
        report.update(
            passed=True, cases=[row["name"] for row in result["events"] if row["name"].startswith("check_")]
        )
    finally:
        if installer:
            installer.close()
        if host:
            host.close()
        scope.close()
        if (root / "installation.json").exists():
            identifier = read(root / "installation.json")["installation"]
            vault = VaultCredentials()
            if vault.get_password(VAULT, identifier) is not None:
                vault.delete_password(VAULT, identifier)
            report["test_credentials_removed"] = vault.get_password(VAULT, identifier) is None
        (evidence / "result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
