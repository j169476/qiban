"""Start/stop an isolated PostgreSQL cluster using the runner's existing binaries."""
import argparse
import json
import os
import secrets
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".dev/ci-postgres"


def command(binary, *arguments, log):
    with log.open("ab") as stream:
        completed = subprocess.run([str(binary), *map(str, arguments)], stdin=subprocess.DEVNULL,
            stdout=stream, stderr=stream, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode:
        raise RuntimeError("Isolated PostgreSQL command failed; inspect its CI-only log")


def start(binary_directory):
    # Preserve a caller-supplied ASCII junction: PostgreSQL on Windows may reject
    # its own installation path when the resolved directory contains non-ASCII.
    binaries = Path(binary_directory).absolute()
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("initdb", "pg_ctl"):
        if not (binaries / (name + suffix)).is_file():
            raise RuntimeError("PostgreSQL binaries are required; database tests cannot be skipped")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if (OUTPUT / "runtime.json").exists():
        raise RuntimeError("An isolated CI cluster is already registered; stop it before starting another")
    temporary = Path(tempfile.mkdtemp(prefix="companion-ci-pg-"))
    token = secrets.token_hex(20)
    (temporary / "owner.json").write_text(json.dumps({"token": token, "repository": str(ROOT)}))
    password = secrets.token_urlsafe(36)
    password_file = temporary / "password"
    password_file.write_text(password, encoding="ascii")
    data, log = temporary / "data", temporary / "postgres.log"
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    runtime = {"root": str(temporary), "binary": str(binaries / ("pg_ctl" + suffix)), "token": token}
    (OUTPUT / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    try:
        command(binaries / ("initdb" + suffix), "-D", data, "-U", "postgres", "--encoding=UTF8",
                "--locale=C", "--auth=scram-sha-256", "--pwfile=" + str(password_file), log=log)
    finally:
        password_file.unlink(missing_ok=True)
    with (data / "postgresql.conf").open("a", encoding="utf-8") as config:
        config.write(f"\nlisten_addresses='127.0.0.1'\nport={port}\nmax_connections=40\n")
    command(Path(runtime["binary"]), "-D", data, "-l", log, "-w", "start", log=temporary / "control.log")
    options = {"host": "127.0.0.1", "port": port, "user": "postgres", "password": password, "dbname": "postgres"}
    with psycopg.connect(**options, autocommit=True) as connection:
        connection.execute("CREATE DATABASE companion_test")
    options["dbname"] = "companion_test"
    path = OUTPUT / "connection.json"
    path.write_text(json.dumps(options), encoding="utf-8")
    print(json.dumps({"ready": True, "connection_file": str(path), "production_used": False}))


def stop():
    path = OUTPUT / "runtime.json"
    if not path.exists():
        return
    runtime = json.loads(path.read_text(encoding="utf-8"))
    temporary = Path(runtime["root"])
    expected = Path(tempfile.gettempdir()).resolve()
    if (temporary.is_symlink() or temporary.resolve().parent != expected
            or not temporary.name.startswith("companion-ci-pg-")):
        raise RuntimeError("CI cluster ownership check failed")
    owner = json.loads((temporary / "owner.json").read_text())
    if owner != {"token": runtime["token"], "repository": str(ROOT)}:
        raise RuntimeError("CI cluster ownership marker does not match")
    if (temporary / "data/postmaster.pid").is_file():
        command(Path(runtime["binary"]), "-D", temporary / "data", "-m", "fast", "-w", "stop",
                log=temporary / "control.log")
    (OUTPUT / "connection.json").unlink(missing_ok=True)
    path.unlink()
    print(json.dumps({"stopped": True, "temporary_logs_retained": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("start", "stop"))
    parser.add_argument("--bin", default=os.environ.get("PGBIN"))
    args = parser.parse_args()
    if args.operation == "stop":
        stop()
    elif args.bin:
        start(args.bin)
    else:
        parser.error("--bin or PGBIN is required")
