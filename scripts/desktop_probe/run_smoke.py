"""Run the desktop probe inside an owned process scope; never use production data."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from companion_core.process_scope import ProcessScope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    executable = args.exe.resolve(strict=True)
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    scope = ProcessScope()
    process = None
    report = {}
    try:
        process = subprocess.Popen(
            [str(executable), "--smoke", "--data-dir", str(evidence)],
            cwd=evidence, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | 0x4,
        )
        scope.attach(process)
        scope.resume()
        report["exit_code"] = process.wait(timeout=60)
        result = json.loads((evidence / "result.json").read_text(encoding="utf8"))
        events = [json.loads(line) for line in (evidence / "logs" / "events.jsonl").read_text(encoding="utf8").splitlines()]
        report["application_exit_graceful"] = (events[-1]["name"] == "shutdown_completed"
            and events[-1]["detail"] == {"state": "stopped", "detail": "graceful_stop"})
        report["passed"] = result["passed"] and report["exit_code"] == 0 and report["application_exit_graceful"]
        report["checks"] = [x["name"] for x in result["events"] if x["name"].startswith("check_")]
    finally:
        scope.close()
        report["owned_process_exited"] = process is not None and process.poll() is not None
        (evidence / "supervision.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
