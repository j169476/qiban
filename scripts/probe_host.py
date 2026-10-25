"""Read-only probe. Never creates, resumes, archives, or sends turns to a chat."""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time


def main():
    report = {
        "codex": shutil.which("codex"),
        "thread_id_available": bool(os.environ.get("CODEX_THREAD_ID")),
        "thread_id": os.environ.get("CODEX_THREAD_ID"),
    }
    executable = report["codex"]
    if not executable:
        print(json.dumps(report))
        return
    proc = subprocess.Popen(
        [executable, "app-server"] + ([] if "--stdio" in sys.argv else ["proxy"]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    messages = queue.Queue()

    def consume():
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass

    threading.Thread(target=consume, daemon=True).start()

    def rpc(method, params, request_id):
        proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        end = time.monotonic() + 5
        while time.monotonic() < end:
            response = messages.get(timeout=max(0.01, end - time.monotonic()))
            if response.get("id") == request_id:
                return response
        raise TimeoutError()

    try:
        init = rpc(
            "initialize",
            {
                "clientInfo": {"name": "bili-host-probe", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
            1,
        )
        report["initialize_ok"] = "result" in init
        if "error" in init:
            report["error"] = init["error"]
        if "result" in init:
            proc.stdin.write('{"method":"initialized","params":{}}\n')
            proc.stdin.flush()
            tid = report["thread_id"]
            if tid:
                answer = rpc("thread/read", {"threadId": tid, "includeTurns": False}, 2)
                thread = answer.get("result", {}).get("thread", {})
                report["current_thread_visible"] = thread.get("id") == tid
                report["cwd_matches"] = os.path.normcase(thread.get("cwd", "")) == os.path.normcase(
                    os.getcwd()
                )
                if "error" in answer:
                    report["read_error"] = answer["error"]
                answer = rpc(
                    "thread/list", {"cwd": os.getcwd(), "archived": False, "limit": 100, "sourceKinds": []}, 3
                )
                report["current_in_active_list"] = any(
                    t.get("id") == tid for t in answer.get("result", {}).get("data", [])
                )
                report["has_more"] = bool(answer.get("result", {}).get("nextCursor"))
                if "error" in answer:
                    report["list_error"] = answer["error"]
    except (OSError, TimeoutError, queue.Empty) as exc:
        report["connection_error"] = type(exc).__name__
    finally:
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
        # Stderr here is CLI startup diagnostics, never credentials or chat content.
        report["diagnostic"] = stderr[-1500:]
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
