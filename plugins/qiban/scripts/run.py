"""Tiny installed-plugin launcher, using the project's isolated runtime."""

import json
import subprocess
import sys
import time
from pathlib import Path

plugin = Path(__file__).resolve().parents[1]
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
settings_file = plugin / "runtime.json"
if not settings_file.exists():
    print('{"error":"setup_required","message":"先在源码仓库执行 python scripts/bootstrap.py"}')
    raise SystemExit(2)
try:
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    python = Path(settings["python"])
    if not all(isinstance(settings[key], str) and settings[key] for key in ("python", "project", "data")):
        raise ValueError()
except (OSError, ValueError, TypeError, KeyError):
    print('{"error":"runtime_config","message":"插件运行配置不可读取或不完整，请重新执行 bootstrap"}')
    raise SystemExit(2) from None
if not python.is_file():
    print('{"error":"runtime_missing","message":"项目运行环境已移动，请重新执行 bootstrap"}')
    raise SystemExit(2)
started = time.monotonic()
control = len(sys.argv) > 1 and sys.argv[1] == "call"
if control:
    print(json.dumps({"qiban_diagnostic": "plugin_entry", "stage": "runtime_launch", "status": "running"}),
          file=sys.stderr, flush=True)
try:
    result = subprocess.run(
        [str(python), "-u", "-m", "qiban", "--project", settings["project"], "--data", settings["data"], *sys.argv[1:]],
        check=False, timeout=120 if control else None,
    )
except subprocess.TimeoutExpired:
    print(json.dumps({"error": "plugin_control_timeout", "message":
        "插件等待已超过 120 秒，操作结果尚未确认。请先运行 doctor 检查服务，再在原管理聊天查询状态；不要伪造聊天身份或直接重复提交。",
        "diagnostics": {"stage": "runtime_wait", "elapsed_ms": round((time.monotonic() - started) * 1000)}}, ensure_ascii=False))
    raise SystemExit(2) from None
except PermissionError:
    print(json.dumps({"error": "execution_denied", "message": "执行环境拒绝启动项目 Python，请在获准的项目终端重试。"}, ensure_ascii=False))
    raise SystemExit(2) from None
except OSError:
    print(json.dumps({"error": "runtime_launch_failed", "message": "项目 Python 未能启动，请检查运行环境或重新执行 bootstrap。"}, ensure_ascii=False))
    raise SystemExit(2) from None
raise SystemExit(result.returncode)
