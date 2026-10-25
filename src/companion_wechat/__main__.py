"""Read-only probe and independent collector CLI; no live-send command."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .probe import TargetSpec, run_probe

_REPORT_FIELDS = {"schema_version", "checked_at", "provider", "environment", "status", "read_only",
                  "messages_sent", "message_content_collected", "account", "targets", "limitations",
                  "dependency_version", "client", "ui", "render_ui"}
_PROBE_STATUSES = {"probe_failed", "unsupported_platform", "dependency_missing", "dependency_unavailable",
                   "client_not_running", "login_required", "main_window_not_visible", "multiple_main_windows",
                   "unrecognized_window_structure", "ui_tree_unavailable", "ui_structure_inconclusive",
                   "ui_visible_identity_unverified", "render_surface_only"}


def load_targets(path):
    if path is None:
        return []
    if path.stat().st_size > 16000:
        raise ValueError("invalid_target_file")
    with path.open(encoding="utf-8") as stream:
        raw = stream.read(16001)
    if len(raw) > 16000:
        raise ValueError("invalid_target_file")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"targets"} or not isinstance(data["targets"], list):
        raise ValueError("invalid_target_file")
    if len(data["targets"]) > 8:
        raise ValueError("invalid_target_file")
    targets = [TargetSpec(**item) for item in data["targets"]]
    if len({target.alias for target in targets}) != len(targets):
        raise ValueError("invalid_target_file")
    return data["targets"]


def isolated_probe(targets, *, timeout=20.0, runner=subprocess.run):
    """Terminate a hung native UIA worker; never publish its raw stdout/stderr."""
    try:
        result = runner([sys.executable, "-m", "companion_wechat", "_probe_worker"],
                        input=json.dumps(targets, ensure_ascii=False), text=True, encoding="utf-8",
                        capture_output=True, timeout=timeout, check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode or len(result.stdout) > 64000:
            raise ValueError("worker_failed")
        report = json.loads(result.stdout)
        if (not isinstance(report, dict) or set(report) - _REPORT_FIELDS
                or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                or report.get("read_only") is not True or type(report.get("messages_sent")) is not int
                or report["messages_sent"] != 0 or report.get("status") not in _PROBE_STATUSES):
            raise ValueError("worker_failed")
        return report
    except subprocess.TimeoutExpired:
        return {"schema_version": 1, "status": "probe_timeout", "read_only": True, "messages_sent": 0}
    except (OSError, ValueError, TypeError):
        return {"schema_version": 1, "status": "probe_worker_failed", "read_only": True, "messages_sent": 0}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "module":
        from .module_cli import main as module_main

        return module_main(argv[1:])
    if argv and argv[0] == "collect":
        from .receiver_cli import main as collector_main

        return collector_main(argv[1:])
    if argv and argv[0] == "db-probe":
        from .db_cli import main as db_main

        return db_main(argv[1:])
    if argv and argv[0] == "visual-probe":
        from .visual_cli import main as visual_main

        return visual_main(argv[1:])
    if argv == ["_probe_worker"]:
        try:
            raw = sys.stdin.read(16001)
            if len(raw) > 16000:
                raise ValueError("invalid_input")
            data = json.loads(raw)
            if not isinstance(data, list) or len(data) > 8:
                raise ValueError("invalid_input")
            targets = [TargetSpec(**item) for item in data]
            report = run_probe(targets)
            print(json.dumps(report, ensure_ascii=False))
            return 0
        except Exception:
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("module", help="正式微信模块的离线配置与构建检查（用 module --help 查看）")
    commands.add_parser("collect", help="独立只收消息到加密 PostgreSQL（用 collect --help 查看参数）")
    commands.add_parser("db-probe", help="受限数据库只读探针（用 db-probe --help 查看参数）")
    commands.add_parser("visual-probe", help="离线本地 OCR 图像可行性探针（用 visual-probe --help 查看参数）")
    probe = commands.add_parser("probe", help="只读检测已打开的微信窗口；不发送、不激活窗口")
    probe.add_argument("--targets", type=Path, help="本机私密目标配置（不要提交版本控制）")
    probe.add_argument("--timeout", type=float, default=20)
    probe.add_argument("--output", type=Path, help="保存脱敏报告")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.timeout <= 60:
            raise ValueError("invalid_timeout")
        report = isolated_probe(load_targets(args.targets), timeout=args.timeout)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["status"] == "ui_visible_identity_unverified" else 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "probe_configuration_error", "read_only": True, "messages_sent": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
