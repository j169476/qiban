"""Opt-in P0 sandbox probes with synthetic data only; never starts the companion.

Run with the project's Python. Output contains booleans and diagnostic codes, not
file contents. The probe targets are created under --output-dir, and no real
account, database, credential or external service is read by the probe payload.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

PAYLOAD = '''import json, pathlib, socket, sys
target, port = pathlib.Path(sys.argv[1]), int(sys.argv[2])
result = {"executed": True}
try:
    result["outside_read"] = bool(target.read_bytes())
except OSError:
    result["outside_read"] = False
try:
    (target.parent / "write-attempt.txt").write_text("synthetic write probe")
    result["outside_write"] = True
except OSError:
    result["outside_write"] = False
try:
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        result["loopback_connect"] = True
except OSError:
    result["loopback_connect"] = False
print(json.dumps(result))
'''


def decode_probe(returncode: int | None, stdout: str, stderr: str) -> dict:
    """Startup failure is inconclusive, never a successful access denial."""
    result = {"status": "not_executed", "returncode": returncode}
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("executed") is True
                and all(type(row.get(k)) is bool for k in (
                    "outside_read", "outside_write", "loopback_connect"))):
            result.update({k: row[k] for k in (
                "outside_read", "outside_write", "loopback_connect")})
            result["status"] = "executed" if returncode == 0 else "execution_failed"
            break
    lower = stderr.lower()
    # Store stable failure categories, not raw diagnostics that could hold secrets.
    if "requires the elevated windows sandbox" in lower:
        result["diagnostic"] = "requires_elevated_windows_backend"
    elif "deny-read" in lower or "read restrictions" in lower or "read access" in lower:
        result["diagnostic"] = "read_restriction_error"
    elif "not support" in lower or "unsupported" in lower:
        result["diagnostic"] = "unsupported_configuration"
    elif "access is denied" in lower or "os error 5" in lower or "拒绝访问" in stderr:
        result["diagnostic"] = "process_access_denied"
    elif returncode != 0:
        result["diagnostic"] = "startup_or_runtime_failure"
    result["isolation_passed"] = result["status"] == "executed" and not any(
        result.get(k, True) for k in ("outside_read", "outside_write", "loopback_connect"))
    return result


def profile_overrides(work: Path) -> list[str]:
    # Absolute paths avoid any extra inherited workspace roots. No write grants.
    fs = {":root": "deny", ":minimal": "read", work.as_posix(): "read",
          Path(sys.base_prefix).as_posix(): "read", Path(sys.executable).parent.parent.as_posix(): "read"}
    table = "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in fs.items()) + "}"
    return ["-c", 'default_permissions="companion-p0"',
            "-c", "permissions.companion-p0.filesystem=" + table,
            "-c", "permissions.companion-p0.network.enabled=false"]


def run(output_dir: Path, backend: str | None = None) -> dict:
    executable = shutil.which("codex")
    if not executable:
        raise SystemExit("Codex CLI is unavailable")
    root = output_dir.resolve() / uuid.uuid4().hex
    work, protected = root / "worker", root / "protected"
    work.mkdir(parents=True)
    protected.mkdir()
    canary = protected / "canary.txt"
    canary.write_text("synthetic-p0-" + uuid.uuid4().hex, encoding="utf-8")
    payload = work / "probe.py"
    payload.write_text(PAYLOAD, encoding="utf-8")
    version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = {"version": version.stdout.strip(), "backend_override": backend, "probes": {}}
    launcher = [executable]
    backend_args = ["-c", "windows.sandbox=" + json.dumps(backend)] if backend else []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        payload_args = [sys.executable, "-I", str(payload), str(canary), str(port)]
        for mode in ("control", "builtin_read_only", "restricted_profile"):
            if mode == "control":
                command = payload_args
            elif mode == "builtin_read_only":
                command = [*launcher, "sandbox", "--include-managed-config", "-C", str(work),
                           "-P", ":read-only", *backend_args, "--", *payload_args]
            else:
                command = [*launcher, "sandbox", "--include-managed-config", "-C", str(work),
                           "-P", "companion-p0", *profile_overrides(work), *backend_args, "--", *payload_args]
            try:
                proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=45, cwd=work,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                result["probes"][mode] = decode_probe(proc.returncode, proc.stdout, proc.stderr)
                # Private local diagnostic only. Never copied into public evidence.
                (root / (mode + ".stderr.txt")).write_text(proc.stderr, encoding="utf-8")
            except subprocess.TimeoutExpired:
                result["probes"][mode] = {"status": "timeout", "isolation_passed": False}
            # Delete exactly the disposable file made by this probe, not a tree.
            (protected / "write-attempt.txt").unlink(missing_ok=True)
    control = result["probes"]["control"]
    result["control_valid"] = control.get("status") == "executed" and all(
        control.get(k) is True for k in ("outside_read", "outside_write", "loopback_connect"))
    result["accepted"] = result["control_valid"] and result["probes"]["restricted_profile"]["isolation_passed"]
    (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".dev/governance-p0/os-probe"))
    parser.add_argument("--backend", choices=("mxc",))
    args = parser.parse_args()
    report = run(args.output_dir, args.backend)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["accepted"] else 1)
