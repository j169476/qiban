"""Exercise the actual Windows form against this project's isolated Ubuntu 24.04 lab."""
import argparse
import base64
import json
import os
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = Path(__file__).resolve().parent
LAB = ROOT / "platforms/linux/server/lab.py"


def lab(name, *args, check=True):
    return subprocess.run([sys.executable, str(LAB), "--name", name, *args],
                          cwd=ROOT, text=True, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120, check=check)


def write_mismatched_key(path, port):
    # A public test vector from RFC 8032, encoded in the OpenSSH public-key blob format.
    alg = b"ssh-ed25519"
    public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    blob = struct.pack(">I", len(alg)) + alg + struct.pack(">I", len(public)) + public
    alias = "127.0.0.1" if port == 22 else f"[127.0.0.1]:{port}"
    path.write_text(f"{alias} ssh-ed25519 {base64.b64encode(blob).decode('ascii')}\n", encoding="ascii")


def verify(name, port):
    config = json.loads(lab(name, "up", "--port", str(port)).stdout)
    exe = CLIENT / "build/Qiban.Remote.exe"
    if not exe.is_file():
        raise RuntimeError("Build the Windows client with build.ps1 first")
    if config["host"] != "127.0.0.1" or config["user"] != "qiban-status":
        raise RuntimeError("A loopback-only project status lab is required")
    evidence = ROOT / ".dev/windows-remote-check" / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
    print(f"Evidence: {evidence}", flush=True)
    wrong = evidence / "wrong_known_hosts"
    write_mismatched_key(wrong, config["port"])
    profile_file = ROOT / ".dev/linux-status" / name / "endpoint.json"
    args = [exe, "--profile", profile_file, "--verify", evidence, "--wrong-hosts", wrong]
    with subprocess.Popen(list(map(str, args))) as process:
        try:
            deadline = time.monotonic() + 50
            while not (evidence / "offline.json").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("First connection failed; see result.json in the evidence directory")
                time.sleep(0.2)
            job = json.loads(lab(name, "enqueue", "--delay", "2").stdout)["id"]
            # Ask the privileged lab harness to inspect the committed index while the GUI is offline.
            deadline = time.monotonic() + 45
            while True:
                inspection = lab(name, "verify", job, check=False)
                if inspection.returncode == 0:
                    break
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Offline fixture did not complete: " + inspection.stderr)
                time.sleep(0.5)
            index = json.loads(inspection.stdout)
            if index["id"] != job or min(index["spans"], index["search_hits"]) < 1:
                raise RuntimeError("Offline index result is incomplete")
            (evidence / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
            signal = evidence / "resume.tmp"
            signal.write_text(job, encoding="ascii")
            os.replace(signal, evidence / "resume.txt")
            if process.wait(timeout=105) != 0:
                raise RuntimeError("Client acceptance failed; see result.json")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    result = json.loads((evidence / "result.json").read_text("utf-8-sig"))
    if not result["passed"]:
        raise RuntimeError("Client acceptance failed")
    print(json.dumps({"result": str(evidence / "result.json"), "passed": True,
                      "before": result["before"]["progress"]["completed"],
                      "after": result["after"]["progress"]["completed"],
                      "job": job, "wrong_key": result["wrong_key_result"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="qiban-linux-remote")
    parser.add_argument("--port", type=int, default=22442)
    args = parser.parse_args()
    try:
        verify(args.name, args.port)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.stderr or str(exc))
