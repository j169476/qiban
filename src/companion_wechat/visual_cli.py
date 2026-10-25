"""Subprocess-bounded, offline image OCR probe. No desktop capture or send command."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_FIELDS = {"schema_version", "mode", "status", "read_only", "ui_accessed", "messages_sent",
           "recognized_text_exported", "image_exported", "content_fingerprints_exported", "text_redacted",
           "engine", "image", "runs", "consistency", "structure", "limitations"}
_STATUSES = {"ok", "no_text_detected", "image_path_invalid", "image_invalid", "image_too_large",
             "image_format_unsupported", "roi_invalid", "dependency_missing", "dependency_version_unsupported",
             "dependency_unavailable", "models_unavailable", "ocr_output_invalid", "ocr_failed", "probe_failed"}


def failure(status):
    return {"schema_version": 1, "mode": "offline_image", "status": status,
            "read_only": True, "ui_accessed": False, "messages_sent": 0,
            "recognized_text_exported": False, "image_exported": False,
            "content_fingerprints_exported": False, "text_redacted": True}


def isolated_visual_probe(image_path, *, roi=None, timeout=30.0, runner=subprocess.run):
    try:
        result = runner([sys.executable, "-m", "companion_wechat.visual_cli", "_worker"],
                        input=json.dumps({"image_path": str(image_path), "roi": roi}),
                        text=True, encoding="utf-8", capture_output=True, timeout=timeout, check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode or len(result.stdout) > 64000:
            raise ValueError
        report = json.loads(result.stdout)
        if (not isinstance(report, dict) or set(report) - _FIELDS or report.get("mode") != "offline_image"
                or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                or report.get("status") not in _STATUSES
                or report.get("read_only") is not True or report.get("ui_accessed") is not False
                or type(report.get("messages_sent")) is not int or report["messages_sent"] != 0
                or report.get("text_redacted") is not True
                or any(report.get(key) is not False for key in
                       ("recognized_text_exported", "image_exported", "content_fingerprints_exported"))):
            raise ValueError
        return report
    except subprocess.TimeoutExpired:
        return failure("visual_probe_timeout")
    except (OSError, ValueError, TypeError):
        return failure("visual_worker_failed")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["_worker"]:
        try:
            raw = sys.stdin.read(16001)
            if len(raw) > 16000:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"image_path", "roi"}:
                raise ValueError
            from .visual_probe import run_visual_probe

            print(json.dumps(run_visual_probe(data["image_path"], roi=data["roi"]), ensure_ascii=False))
            return 0
        except Exception:
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True, help="本地静态图片；不访问微信窗口")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, help="仅保存脱敏统计，不保存识别文字")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.timeout <= 60 or (args.output and args.image.resolve() == args.output.resolve()):
            raise ValueError
        report = isolated_visual_probe(args.image, roi=args.roi, timeout=args.timeout)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["status"] == "ok" else 2
    except (OSError, ValueError, TypeError):
        print(json.dumps(failure("visual_configuration_error")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
