"""Verify owner death cleanup before the independent test safety Job is closed."""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from companion_core.process_scope import ProcessScope

api = ctypes.WinDLL("kernel32", use_last_error=True)
api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
api.OpenProcess.restype = wintypes.HANDLE
api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
api.WaitForSingleObject.restype = wintypes.DWORD
api.CloseHandle.argtypes = [wintypes.HANDLE]
api.CloseHandle.restype = wintypes.BOOL


def start(exe, arguments, scope):
    process = subprocess.Popen([str(exe), *arguments], stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
    scope.attach(process)
    scope.resume()
    return process


def read_ready(path, pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            data = json.loads(path.read_text(encoding="utf8"))
            if data["pid"] == pid:
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.02)
    raise TimeoutError("Owner/worker did not publish readiness")


def check_tree(exe, directory, crash, unrelated):
    directory.mkdir()
    scope = ProcessScope()
    handles = []
    process = None
    try:
        process = start(exe, ["--lifecycle-owner", str(directory)], scope)
        owner = read_ready(directory / "owner.json", process.pid)
        worker = read_ready(directory / "worker.json", owner["child"])
        assert worker["received"] == ["", "a b", 'quote"value', "end\\"]
        for pid in [owner["child"], worker["child"]]:
            handle = api.OpenProcess(0x100000, False, pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            assert api.WaitForSingleObject(handle, 0) == 258
        if crash:
            process.kill()  # Kill only the owner, not its tree or the safety Job.
        else:
            (directory / "stop").write_text("stop", encoding="utf8")
        code = process.wait(timeout=10)
        if not crash:
            assert code == 0
        exited = [api.WaitForSingleObject(handle, 5000) == 0 for handle in handles]
        assert all(exited), "Owned child or descendant survived the owner"
        assert unrelated.poll() is None, "Unrelated same-executable process was stopped"
        return {"mode": "owner_killed" if crash else "normal_exit", "exit_code": code,
                "child_and_descendant_exited": exited, "unrelated_alive": True,
                "verified_before_safety_job_cleanup": True, "argument_roundtrip": True}
    finally:
        for handle in handles:
            api.CloseHandle(handle)
        scope.close()


def check_window(exe, directory, unrelated):
    directory.mkdir()
    scope = ProcessScope()
    handles = []
    try:
        process = start(exe, ["--lifecycle-window", "--data-dir", str(directory)], scope)
        deadline = time.monotonic() + 20
        ready = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Window exited before service readiness")
            try:
                for line in (directory / "logs" / "events.jsonl").read_text(encoding="utf8").splitlines():
                    event = json.loads(line)
                    if event["name"] == "lifecycle_window_ready":
                        ready = event["detail"]
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if ready:
                break
            time.sleep(0.05)
        assert ready is not None, "Window did not become ready"
        for pid in [ready["child"], ready["browser"]]:
            handle = api.OpenProcess(0x100000, False, pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
        process.kill()
        process.wait(timeout=10)
        exits = [api.WaitForSingleObject(handle, 10000) == 0 for handle in handles]
        assert all(exits), "Window backend or WebView browser survived owner death"
        assert unrelated.poll() is None
        return {"backend_exited": exits[0], "webview_browser_exited": exits[1],
                "service_ready_before_owner_kill": True, "unrelated_alive": True,
                "verified_before_safety_job_cleanup": True}
    finally:
        for handle in handles:
            api.CloseHandle(handle)
        scope.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    exe, evidence = args.exe.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    unrelated_scope = ProcessScope()
    report = {"cases": [], "passed": False}
    unrelated = None
    try:
        unrelated = start(exe, ["--lifecycle-leaf"], unrelated_scope)
        for i, crash in enumerate([False, True, True, True, False]):
            report["cases"].append(check_tree(exe, evidence / f"case-{i}", crash, unrelated))
        invalid_scope = ProcessScope()
        try:
            invalid = start(exe, ["--lifecycle-invalid", str(evidence)], invalid_scope)
            assert invalid.wait(timeout=10) == 0
            report["missing_executable_rejected"] = True
        finally:
            invalid_scope.close()
        report["window_crash"] = check_window(exe, evidence / "window", unrelated)
        report["passed"] = True
    finally:
        unrelated_scope.close()
        report["unrelated_test_process_cleaned"] = unrelated is not None and unrelated.poll() is not None
        (evidence / "result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
