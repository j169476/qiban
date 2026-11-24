"""Provision the lab-only local PostgreSQL cluster, then supervise SSH and the worker."""
import os
import pwd
import subprocess
from pathlib import Path

import supervisor

DATA = Path("/var/lib/qiban-pg")
SOCKET = "/run/postgresql"
BIN = Path("/usr/lib/postgresql/16/bin")


def pg(executable, *args, **options):
    user = pwd.getpwnam("postgres")
    return subprocess.run([str(BIN / executable), *map(str, args)], check=True, user=user.pw_uid,
                          group=user.pw_gid, extra_groups=[], cwd="/", text=True, **options)


def exists(query):
    return pg("psql", "-XAt", "-h", SOCKET, "-d", "postgres", "-c", query, capture_output=True).stdout.strip() == "1"


def main():
    user = pwd.getpwnam("postgres")
    for path, mode in [(DATA, 0o700), (Path(SOCKET), 0o755)]:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, user.pw_uid, user.pw_gid)
        path.chmod(mode)
    if not (DATA / "PG_VERSION").exists():
        pg("initdb", "-D", DATA, "--encoding=UTF8", "--locale=C", "--auth-local=peer", "--auth-host=reject")
    pg("pg_ctl", "-D", DATA, "-l", DATA / "server.log", "-w", "-o",
       f"-k {SOCKET} -c listen_addresses='' -c max_connections=24 -c shared_buffers=64MB", "start")
    try:
        if not exists("SELECT 1 FROM pg_roles WHERE rolname='qiban'"):
            pg("createuser", "-h", SOCKET, "--no-superuser", "--no-createdb", "--no-createrole", "qiban")
        if not exists("SELECT 1 FROM pg_database WHERE datname='qiban_synthetic'"):
            pg("createdb", "-h", SOCKET, "-O", "qiban", "qiban_synthetic")
        return supervisor.main("/opt/qiban/dialogue_service.py")
    finally:
        pg("pg_ctl", "-D", DATA, "-m", "fast", "-w", "stop")


if __name__ == "__main__":
    raise SystemExit(main())
