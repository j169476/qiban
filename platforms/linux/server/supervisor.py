"""Container init. Only sshd and host-key provisioning need elevated OS rights."""
import grp
import os
import pwd
import signal
import subprocess
import sys
import time
from pathlib import Path


def main(worker_script="/opt/qiban/service.py"):
    worker_user = pwd.getpwnam("qiban")
    reader_gid = grp.getgrnam("qiban-status").gr_gid
    for name, owner, group, mode in (
        ("/run/sshd", 0, 0, 0o755),
        ("/run/qiban", worker_user.pw_uid, reader_gid, 0o2750),
        ("/var/lib/qiban", worker_user.pw_uid, worker_user.pw_gid, 0o700),
        ("/var/lib/qiban-hostkey", 0, 0, 0o700),
    ):
        path = Path(name)
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, owner, group)
        os.chmod(path, mode)
    key_path = Path("/var/lib/qiban-hostkey/ssh_host_ed25519_key")
    if not key_path.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)], check=True)
    public = Path("/run/test-client.pub").read_text("ascii").strip().split()
    if len(public) < 2 or public[0] != "ssh-ed25519" or len(public[1]) > 256:
        raise ValueError("Expected one test client's public Ed25519 key")
    authorized = Path("/etc/qiban/authorized_keys")
    authorized.write_text(f"restrict {public[0]} {public[1]}\n", encoding="ascii")
    authorized.chmod(0o644)
    config = "/etc/qiban/sshd_config"
    subprocess.run(["/usr/sbin/sshd", "-t", "-f", config], check=True)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = []
    try:
        children.append(subprocess.Popen([sys.executable, worker_script], user=worker_user.pw_uid,
                                         group=worker_user.pw_gid, extra_groups=[], umask=0o077))
        children.append(subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", config]))
        while not stopping:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(0.2)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
