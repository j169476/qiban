"""Run each unit-test file in exactly one deterministic CI shard."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def selection(index, count):
    files = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").rglob("test_*.py"))
    if not 0 <= index < count <= len(files):
        raise ValueError("Invalid CI shard")
    return files[index::count]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    selected = selection(args.index, args.count)
    if args.list:
        print(json.dumps(selected))
    else:
        raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", *selected,
            f"--junitxml=.dev/unit-{args.index}.xml", "-o", "faulthandler_timeout=120", "--durations=10"], cwd=ROOT))
