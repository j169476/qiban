"""Forced SSH command. Relay bounded JSON lines to the service's read-only socket."""
import os
import socket
import sys


def main():
    if os.environ.get("SSH_ORIGINAL_COMMAND", "") not in ("", "qiban-status"):
        print("Only qiban-status is available.", file=sys.stderr)
        return 2
    limit = 65536
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(10)
        conn.connect("/run/qiban/status.sock")
        with conn.makefile("rb") as replies:
            while line := sys.stdin.buffer.readline(limit + 1):
                if len(line) > limit or not line.endswith(b"\n"):
                    return 2
                conn.sendall(line)
                reply = replies.readline(limit + 1)
                if not reply.endswith(b"\n") or len(reply) > limit:
                    return 1
                sys.stdout.buffer.write(reply)
                sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError:
        print("Status service unavailable.", file=sys.stderr)
        raise SystemExit(1)
