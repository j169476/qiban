"""Exercise the real SSH path, offline task commit, OS boundary and cold process identity."""
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from lab import CONSOLE_KEY, message, run, ssh_tool


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def error_code(reply, expected):
    require(not reply["ok"] and reply["error"] == expected, f"Expected {expected}")


def ssh_denials(lab):
    hello = message("protocol.hello", {"versions": [1]})
    denied = lab.exchange([message("runtime.status"), message("protocol.hello", {"versions": [2]}),
                           hello, message("tasks.enqueue")])
    for reply, expected in zip((denied[0], denied[1], denied[3]),
                               ("handshake_required", "protocol_incompatible", "operation_not_allowed"), strict=True):
        error_code(reply, expected)
    require(denied[2]["ok"], "v1 negotiation should succeed")
    profile = json.loads(lab.profile.read_text("utf-8"))
    with tempfile.TemporaryDirectory(dir=lab.folder) as folder:
        tmp = Path(folder)
        run(ssh_tool("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-f", tmp / "other_key")
        key = (tmp / "other_key.pub").read_text("ascii").split()
        wrong_host = tmp / "wrong_hosts"
        wrong_host.write_text(f"[127.0.0.1]:{profile['port']} {key[0]} {key[1]}\n", encoding="ascii")
        mismatch = run(*lab.ssh(known=str(wrong_host.relative_to(lab.folder))), input="", cwd=lab.folder, check=False)
        unknown = run(*lab.ssh(identity=str((tmp / "other_key").relative_to(lab.folder))),
                      input="", cwd=lab.folder, check=False)
        require(mismatch.returncode != 0 and "host key verification failed" in mismatch.stderr.lower(),
                "Expected rejection of a different host key")
        require(unknown.returncode != 0 and "permission denied" in unknown.stderr.lower(),
                "Expected rejection of an unregistered user key")
    wrong = run(*lab.ssh(command="other-command"), input="", cwd=lab.folder, check=False)
    require(wrong.returncode != 0 and "Only qiban-status" in wrong.stderr, "Forced command was not enforced")
    return ["unnegotiated", "incompatible_version", "unlisted_operation", "different_host_key",
            "unregistered_client_key", "other_remote_command"]


def os_evidence(lab):
    def cg(name):
        return lab.docker("cat", "/sys/fs/cgroup/" + name, user="qiban")
    release = lab.docker("cat", "/etc/os-release", user="qiban")
    version = dict(line.split("=", 1) for line in release.splitlines() if "=" in line)
    cpu = cg("cpu.max")
    quota, period = map(int, cpu.split())
    limit = int(cg("memory.max"))
    require(version["ID"].strip('"') == "ubuntu" and version["VERSION_ID"].strip('"') == "24.04",
            "Expected Ubuntu 24.04 userland")
    require(quota == 2 * period and limit == 4 * 1024**3, "Unexpected actual cgroup limits")
    uid = int(lab.docker("id", "-u", user="qiban-status"))
    require(uid != 0, "SSH reader must be unprivileged")
    data_read = run("docker", "exec", "--user", "qiban-status", lab.name, "test", "-r", "/var/lib/qiban/tasks.sqlite3",
                    check=False)
    require(data_read.returncode == 1, "SSH reader can read the private task store")
    peak = run("docker", "exec", "--user", "qiban", lab.name, "cat", "/sys/fs/cgroup/memory.peak", check=False)
    return {"os": version["PRETTY_NAME"].strip('"'), "kernel": lab.docker("uname", "-r"), "cpu_max": cpu,
            "memory_max_bytes": limit, "memory_current_bytes": int(cg("memory.current")),
            "memory_peak_bytes": int(peak.stdout) if peak.returncode == 0 else None,
            "memory_events": cg("memory.events"), "reader_uid": uid, "reader_can_read_task_db": False,
            "image_id": lab.inspect()["Image"]}


def accept(lab):
    first = lab.status()
    require(all(not p["available"] for p in first["platforms"].values()), "Unexpected live platform")
    denied = ssh_denials(lab)
    job = lab.fixture("enqueue", "--delay", "6")["id"]
    queued = lab.status()  # exchange() waits for ssh to exit, leaving no management connection here.
    require(queued["progress"]["queued"] >= 1, "Job was not sampled before its due time")
    disconnected_at = datetime.now(timezone.utc).isoformat()
    time.sleep(7)
    reconnected = lab.status()
    task = lab.fixture("verify", job)
    require(reconnected["runtime_instance"] == first["runtime_instance"], "Worker changed during disconnect")
    require(reconnected["heartbeat_count"] > queued["heartbeat_count"] and
            reconnected["progress"]["completed"] > queued["progress"]["completed"] and
            reconnected["progress"]["last_completed_id"] == job, "Offline completion was not observed")
    before_restart = os_evidence(lab)
    restart_job = lab.fixture("enqueue", "--delay", "4")["id"]
    run("docker", "restart", lab.name)
    restarted = lab.wait_ready()
    require(restarted["installation_id"] == first["installation_id"] and
            restarted["runtime_instance"] != first["runtime_instance"], "Restart identity mismatch")
    for _ in range(30):
        restarted = lab.status()
        if restarted["progress"]["last_completed_id"] == restart_job:
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("Pending job did not recover across restart")
    require(lab.fixture("verify", job) == task, "Index changed across restart")
    report = {"lab": lab.name, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "os_before_restart": before_restart, "os_after_restart": os_evidence(lab),
              "host_key_fingerprint": lab.docker("ssh-keygen", "-lf", CONSOLE_KEY + ".pub").split()[1],
              "denials_verified": denied, "disconnected_at": disconnected_at, "before_disconnect": queued,
              "after_reconnect": reconnected, "verified_index": task, "after_restart": restarted,
              "recovered_index": lab.fixture("verify", restart_job)}
    (lab.folder / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
