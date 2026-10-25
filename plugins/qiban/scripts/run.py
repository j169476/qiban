"""Tiny installed-plugin launcher, using the project's isolated runtime."""

import json
import subprocess
import sys
from pathlib import Path

plugin = Path(__file__).resolve().parents[1]
settings_file = plugin / "runtime.json"
if not settings_file.exists():
    print('{"error":"setup_required","message":"先在源码仓库执行 python scripts/bootstrap.py"}')
    raise SystemExit(2)
settings = json.loads(settings_file.read_text(encoding="utf-8"))
python = Path(settings["python"])
if not python.is_file():
    print('{"error":"runtime_missing","message":"项目运行环境已移动，请重新执行 bootstrap"}')
    raise SystemExit(2)
result = subprocess.run(
    [
        str(python),
        "-m",
        "qiban",
        "--project",
        settings["project"],
        "--data",
        settings["data"],
        *sys.argv[1:],
    ],
    check=False,
)
raise SystemExit(result.returncode)
