"""Local Ubuntu 24.04 acceptance environment. Only 'status' needs no Docker access."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
IMAGE_TAG = os.environ.get("QIBAN_LINUX_IMAGE_TAG", "local")
LABEL = "org.qiban.local-status-lab"
CONSOLE_KEY = "/var/lib/qiban-hostkey/ssh_host_ed25519_key"
SSH_USER = "qiban-status"


def run(*args, check=True, timeout=45, **kwargs):
    return subprocess.run(list(map(str, args)), check=check, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **kwargs)


def ssh_tool(tool):
    if os.name == "nt":
        for folder in ("System32", "Sysnative"):
            path = Path(os.environ["SystemRoot"], folder, "OpenSSH", tool + ".exe")
            if path.is_file():
                return str(path)
    found = shutil.which(tool)
    if not found:
        raise RuntimeError(f"{tool} not found")
    return found


class Lab:
    image = f"qiban-linux-status:{IMAGE_TAG}"
    volumes = (("-data", "/var/lib/qiban"), ("-hostkey", "/var/lib/qiban-hostkey"))

    def __init__(self, name):
        if not re.fullmatch(r"qiban-linux-[a-z0-9-]{1,30}", name):
            raise ValueError("Use a qiban-linux- prefixed lab name")
        self.name = name
        self.folder = ROOT / ".dev" / "linux-status" / name
        self.profile = self.folder / "endpoint.json"

    def inspect(self):
        result = run("docker", "inspect", "--type", "container", self.name, check=False)
        if result.returncode:
            if "No such" in result.stderr:
                return None
            result.check_returncode()
        data, = json.loads(result.stdout)
        if (data["Config"]["Labels"] or {}).get(LABEL) != self.name:
            raise RuntimeError("Existing container is not this project's lab")
        return data

    def docker(self, *args, user=None):
        return run("docker", "exec", *(["--user", user] if user else []), self.name, *args).stdout.strip()

    def up(self, port):
        if not 1024 <= port <= 65535:
            raise ValueError("Expected a local port between 1024 and 65535")
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = self.folder / "id_ed25519"
        if not key.exists():
            run(ssh_tool("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", self.name, "-f", key)
        data = self.inspect()
        if data:
            if not data["State"]["Running"]:
                run("docker", "start", self.name)
        else:
            # Reject collisions with foreign volumes before mounting either of them.
            mounts = []
            for suffix, destination in self.volumes:
                name = self.name + suffix
                mounts.extend(["--mount", f"type=volume,source={name},target={destination}"])
                found = run("docker", "volume", "inspect", name, check=False)
                if found.returncode == 0:
                    if (json.loads(found.stdout)[0].get("Labels") or {}).get(LABEL) != self.name:
                        raise RuntimeError("Existing volume belongs to a different project")
                elif "no such volume" in found.stderr.lower():
                    run("docker", "volume", "create", "--label", LABEL + "=" + self.name, name)
                else:
                    found.check_returncode()
            run("docker", "run", "--detach", "--init", "--name", self.name, "--label", LABEL + "=" + self.name,
                "--cpus", "2", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "128",
                "--publish", f"127.0.0.1:{port}:22",
                *mounts, "--mount", f"type=bind,source={key}.pub,target=/run/test-client.pub,readonly", self.image)
        binding, = self.inspect()["NetworkSettings"]["Ports"]["22/tcp"]
        if binding["HostIp"] != "127.0.0.1":
            raise RuntimeError("Expected a loopback-only port")
        port = int(binding["HostPort"])
        for _ in range(40):
            ready = run("docker", "exec", self.name, "test", "-S", "/run/qiban/status.sock", check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Service did not create its socket; inspect project container logs")
        pubkey = self.docker("ssh-keygen", "-y", "-f", CONSOLE_KEY).split()
        trusted = f"[127.0.0.1]:{port} {pubkey[0]} {pubkey[1]}\n"
        known = self.folder / "known_hosts"
        if known.exists() and known.read_text("ascii") != trusted:
            raise RuntimeError("Pinned host key or port changed; inspect the lab before establishing new trust")
        known.write_text(trusted, encoding="ascii")
        endpoint = {"host": "127.0.0.1", "port": port, "user": SSH_USER, "identity_file": str(key),
                    "known_hosts": str(known), "fingerprint": self.docker("ssh-keygen", "-lf", CONSOLE_KEY + ".pub").split()[1]}
        self.profile.write_text(json.dumps(endpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        self.wait_ready()
        return endpoint

    def ssh(self, known="known_hosts", identity="id_ed25519", command="qiban-status"):
        profile = json.loads(self.profile.read_text("utf-8"))
        return [ssh_tool("ssh"), "-T", "-F", "none", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=none",
                "-o", f"UserKnownHostsFile={known}", "-o", "ConnectTimeout=5", "-i", identity,
                "-p", str(profile["port"]), SSH_USER + "@127.0.0.1", command]

    def exchange(self, requests):
        encoded = "".join(json.dumps(r) + "\n" for r in requests)
        result = run(*self.ssh(), input=encoded, cwd=self.folder, timeout=10)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        if [r["request_id"] for r in requests] != [r["request_id"] for r in replies]:
            raise RuntimeError("SSH response IDs did not match requests")
        return replies

    def status(self):
        hello, reply = self.exchange([message("protocol.hello", {"versions": [1]}), message("runtime.status")])
        if not hello["ok"] or not reply["ok"]:
            raise RuntimeError("SSH read failed")
        if (hello["result"]["operations"] != ["runtime.status"] or
                any(hello["result"][k] != reply["result"][k] for k in ("runtime_instance", "installation_id"))):
            raise RuntimeError("Unexpected negotiated service identity or capabilities")
        return reply["result"]

    def fixture(self, *args):
        return json.loads(self.docker("python3", "/opt/qiban/jobs.py", *args, user="qiban"))

    def wait_ready(self):
        deadline = time.monotonic() + 15
        while True:
            try:
                return self.status()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.3)

    def require_lab(self):
        if self.inspect() is None:
            raise RuntimeError("Run up first")


def message(op, arguments=None):
    return {"action": "management", "operation": op, "arguments": arguments or {}, "request_id": uuid.uuid4().hex}


def build_status_image():
    subprocess.run(["docker", "build", "--tag", Lab.image, "--file", "platforms/linux/server/Dockerfile", "."],
                   cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="qiban-linux-round1")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("build")
    commands.add_parser("up").add_argument("--port", type=int, default=22441)
    for action in ("stop", "restart", "status", "accept"):
        commands.add_parser(action)
    commands.add_parser("enqueue").add_argument("--delay", type=int, default=5)
    commands.add_parser("verify").add_argument("id")
    args = parser.parse_args()
    lab = Lab(args.name)
    if args.action == "build":
        build_status_image()
        return
    if args.action == "up":
        result = lab.up(args.port)
    elif args.action == "status":
        result = lab.status()
    else:
        lab.require_lab()
        if args.action == "accept":
            from acceptance import accept
            result = accept(lab)
        elif args.action == "restart":
            run("docker", "restart", lab.name)
            result = lab.wait_ready()
        elif args.action == "stop":
            result = run("docker", "stop", lab.name).stdout.strip()
        elif args.action == "enqueue":
            result = lab.fixture("enqueue", "--delay", str(args.delay))
        else:
            result = lab.fixture("verify", args.id)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit((exc.stderr or str(exc)).strip())
