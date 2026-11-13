"""Bounded Windows probe lifetime with truthful forced-cleanup reporting.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from companion_core.process_scope import ProcessScope


def read_updates(path, offset):
    """Read complete appended records; leave a partial final line for next read."""
    updates = []
    with Path(path).open("rb") as source:
        source.seek(offset)
        while True:
            line = source.readline()
            if not line.endswith(b"\n"):
                break
            updates.append(json.loads(line.decode("utf8")))
            offset = source.tell()
    return updates, offset


def supervise(command, *, directory, prefix, terminal_statuses, timeout=90, exit_grace=10, interrupt_at=None):
    directory = Path(directory).resolve()
    status_path = directory / (prefix + "-status.jsonl")
    if any(
        (directory / (prefix + suffix)).exists()
        for suffix in ("-status.jsonl", "-supervision.json", "-stdout.log", "-stderr.log")
    ):
        raise ValueError("Use a fresh evidence prefix; prior evidence must not be overwritten")
    scope = ProcessScope()
    proc = None
    result = {"probe_status": "not_started", "owned_process_exited": False}
    failure = None
    offset = 0
    try:
        with (
            (directory / (prefix + "-stdout.log")).open("wb") as out,
            (directory / (prefix + "-stderr.log")).open("wb") as err,
        ):
            proc = subprocess.Popen(
                command,
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            scope.attach(proc)
            scope.resume()
            result["pid"] = proc.pid
            started = time.monotonic()
            terminal_at = None
            while proc.poll() is None:
                now = time.monotonic()
                if status_path.exists():
                    updates, offset = read_updates(status_path, offset)
                    if updates:
                        state = updates[-1]
                        result["probe_status"] = state.get("status")
                        result["probe_state"] = state
                    if result["probe_status"] in terminal_statuses and terminal_at is None:
                        terminal_at = now
                    if result["probe_status"] == interrupt_at:
                        result["forced_cleanup_reason"] = "injected_interruption"
                        break
                if terminal_at is not None and now - terminal_at >= exit_grace:
                    result["forced_cleanup_reason"] = "exit_deadline"
                    break
                if now - started >= timeout:
                    result["forced_cleanup_reason"] = "probe_deadline"
                    break
                time.sleep(0.05)
            if status_path.exists():
                updates, offset = read_updates(status_path, offset)
                if updates:
                    result["probe_state"] = updates[-1]
                    result["probe_status"] = updates[-1].get("status")
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            result["exited_before_cleanup"] = proc.poll() is not None
    except BaseException as exc:
        failure = exc
        result["supervisor_error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        try:
            scope.close()
            result["owned_process_exited"] = proc is not None and proc.poll() is not None
        except BaseException as exc:
            failure = failure or exc
            result["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
        result["child_exit_code"] = proc.poll() if proc is not None else None
        result["graceful_exit_verified"] = (
            bool(result.get("exited_before_cleanup")) and result["child_exit_code"] == 0
        )
        (directory / (prefix + "-supervision.json")).write_text(json.dumps(result, indent=2), encoding="utf8")
    if failure is not None:
        raise failure
    return result
