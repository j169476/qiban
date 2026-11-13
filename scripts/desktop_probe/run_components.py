"""Run real child-process component lifecycle cases with bounded outer cleanup."""
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
    parser.add_argument("--group", action="store_true")
    args = parser.parse_args()
    exe, evidence = args.exe.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    scope = ProcessScope()
    try:
        process = subprocess.Popen([str(exe), "--group-tests" if args.group else "--component-tests", str(evidence)],
                                   cwd=evidence, stdin=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
        scope.attach(process)
        scope.resume()
        code = process.wait(timeout=60)
        result = json.loads((evidence / ("group-result.json" if args.group else "component-result.json")).read_text(encoding="utf8"))
        print(json.dumps(result))
        assert result["passed"] and code == 0
    finally:
        scope.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
