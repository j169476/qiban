"""Windows R0 file worker with time and memory limits; no production integration."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from companion_core.process_scope import ExtendedLimits, ProcessScope


def run(input_path, output, dependencies, timeout=20, memory_mb=512):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env.update(PYTHONPATH=str(Path(dependencies).resolve()), PYTHONUTF8="1",
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    scope, process = ProcessScope(), None
    report = {"timed_out": False, "memory_limit_mb": memory_mb}
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x200  # Kill-on-close and total Job memory.
        limits.job_memory = memory_mb * 1024 * 1024
        if not scope.api.SetInformationJobObject(scope.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        with (output / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("reader.py")),
                 str(Path(input_path).resolve()), str(output / "result.json")],
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x4,
            )
            scope.attach(process)
            scope.resume()
            try:
                report["exit_code"] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                report["timed_out"] = True
    finally:
        scope.close()
        report["owned_process_exited"] = process is not None and process.poll() is not None
        (output / "supervision.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    result = run(args.input, args.evidence, args.dependencies, args.timeout)
    print(json.dumps(result))
    raise SystemExit(0 if result.get("exit_code") == 0 and not result["timed_out"] else 1)
