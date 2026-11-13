"""Run a local speech probe with owned-process cleanup and a finite deadline."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from companion_core.process_scope import ProcessScope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("worker_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(args.dependencies.resolve())
    env["PYTHONUTF8"] = "1"
    worker_args = args.worker_args
    if worker_args and worker_args[0] == "--":
        worker_args = worker_args[1:]
    report = {"timed_out": False}
    scope = ProcessScope()
    process = None
    try:
        with (evidence / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("worker.py")), *worker_args,
                 "--output", str(evidence)], env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW | 0x4,
            )
            scope.attach(process)
            scope.resume()
            try:
                report["exit_code"] = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                report["timed_out"] = True
    finally:
        scope.close()
        report["owned_process_exited"] = process is not None and process.poll() is not None
        (evidence / "supervision.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report))
    return 0 if report.get("exit_code") == 0 and not report["timed_out"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
