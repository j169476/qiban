"""Opt-in read-only database probe, with a hard-bounded private worker."""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .db_probe import ProbeError, base_report, validate_config

_STATUSES = {
    "preparing", "ready", "observing", "watch_complete", "configuration_invalid",
    "database_path_invalid", "database_format_unsupported", "contact_schema_unsupported",
    "target_missing_or_ambiguous", "target_kind_mismatch", "message_schema_unsupported",
    "sender_schema_unsupported", "database_generation_changed", "observation_limit_exceeded",
    "message_identity_conflict", "account_missing_or_ambiguous", "database_count_unsupported",
    "target_message_table_missing", "unsupported_platform", "invalid_configuration",
    "client_not_running", "client_ambiguous", "client_version_unsupported", "client_identity_changed",
    "process_access_denied", "process_closed", "read_budget_exceeded", "scan_timeout",
    "scan_limit_exceeded", "process_query_failed", "invalid_memory_address", "key_not_found",
    "key_scan_failed", "invalid_database_page", "window_query_failed", "key_closed",
    "unknown_key_alias", "dependency_missing", "dependency_version_unsupported",
    "database_read_failed", "database_sidecars_unavailable", "database_open_failed",
    "database_query_failed", "probe_failed", "probe_timeout", "worker_failed", "interrupted",
    "path_invalid", "source_unavailable", "sidecars_unavailable", "key_invalid",
    "dependency_unavailable", "database_unreadable", "database_busy", "query_denied",
    "query_timeout", "query_failed", "connection_closed", "worker_cleanup_failed",
}
_COUNTERS = {"account_candidates", "account_matches", "message_sources", "polls", "minimized_samples",
             "foreground_target_samples", "elapsed_seconds", "max_poll_seconds", "cpu_seconds"}
_TARGET_FIELDS = {"new_rows", "marker_counts", "minimized_marker_counts",
                  "unverified_sender_markers", "unstable_id_markers"}


def failure(code):
    report = base_report()
    report["status"] = code if code in _STATUSES else "probe_failed"
    return report


def validate_report(report):
    """Allowlist the entire tree, not only the outer object."""
    base = base_report()
    if not isinstance(report, dict) or set(report) - (set(base) | _COUNTERS |
                                                     {"client_version", "prefix", "targets"}):
        raise ValueError("invalid_report")
    for key, value in base.items():
        if key == "status":
            if not isinstance(report.get(key), str) or report[key] not in _STATUSES:
                raise ValueError("invalid_report")
        elif key not in report or type(report[key]) is not type(value) or report[key] != value:
            raise ValueError("invalid_report")
    for key in _COUNTERS & set(report):
        value = report[key]
        kinds = {int, float} if key in {"elapsed_seconds", "max_poll_seconds", "cpu_seconds"} else {int}
        if type(value) not in kinds or not 0 <= value <= 100000:
            raise ValueError("invalid_report")
    if "client_version" in report and report["client_version"] != "4.1.15.13":
        raise ValueError("invalid_report")
    if "prefix" in report and (not isinstance(report["prefix"], str) or
                               not re.fullmatch(r"XLD-[A-F0-9]{8}", report["prefix"])):
        raise ValueError("invalid_report")
    if "targets" in report:
        targets = report["targets"]
        if not isinstance(targets, dict) or set(targets) != {"owner_main", "family"}:
            raise ValueError("invalid_report")
        for target in targets.values():
            if not isinstance(target, dict) or set(target) != _TARGET_FIELDS:
                raise ValueError("invalid_report")
            for key, value in target.items():
                values = value if key.endswith("counts") else [value]
                if not isinstance(values, list) or (key.endswith("counts") and len(values) != 3):
                    raise ValueError("invalid_report")
                if any(type(v) is not int or not 0 <= v <= 10000 for v in values):
                    raise ValueError("invalid_report")
    if report["status"] in {"ready", "observing", "watch_complete"} and not (
            _COUNTERS | {"client_version", "prefix", "targets"}) <= set(report):
        raise ValueError("invalid_report")
    return report


def load_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        raw = stream.read(16001)
    if len(raw) > 16000:
        raise ProbeError("configuration_invalid")
    return json.loads(raw)


def load_config(locations, targets):
    roots, intent = load_json(locations), load_json(targets)
    if (not isinstance(roots, dict) or "account_dirs" not in roots
            or set(roots) - {"account_dirs", "client_pid"} or not isinstance(intent, dict)):
        raise ProbeError("configuration_invalid")
    if intent.get("automatic_sending_enabled") is not False:
        raise ProbeError("configuration_invalid")
    config = {"account_dirs": roots["account_dirs"],
              "private_hint": intent["private"]["wechat_id_hint"],
              "group_hint": intent["group"]["window_title_hint"]}
    if "client_pid" in roots:
        config["client_pid"] = roots["client_pid"]
    validate_config(config)
    return config


def isolated(config, *, duration=0, interval=1, prefix="XLD-00000000", emit=None, startup_timeout=150):
    payload = {"config": config, "duration": duration, "interval": interval, "prefix": prefix}
    child = thread = writer = None
    last = failure("worker_failed")
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "companion_wechat.db_cli", "_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        reports = queue.Queue(maxsize=16)
        started = time.monotonic()

        def write_input():
            try:
                child.stdin.write(json.dumps(payload, ensure_ascii=False))
                child.stdin.close()
            except (OSError, ValueError):
                try:
                    reports.put(None, timeout=0.1)
                except queue.Full:
                    pass

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()

        def reader():
            try:
                while line := child.stdout.readline(64001):
                    if len(line) > 64000:
                        reports.put(None, timeout=1)
                        return
                    reports.put(line, timeout=2)
                reports.put(None, timeout=2)
            except (OSError, ValueError, queue.Full):
                return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        ready = False
        while time.monotonic() - started < startup_timeout + duration + 10:
            if not ready and time.monotonic() - started > startup_timeout:
                return failure("probe_timeout")
            try:
                raw = reports.get(timeout=0.5)
            except queue.Empty:
                if child.poll() is not None:
                    break
                continue
            if raw is None:
                break
            last = validate_report(json.loads(raw))
            ready = ready or last["status"] == "ready"
            if emit:
                emit(last)
        else:
            return failure("probe_timeout")
        child.wait(timeout=3)
        if child.returncode != 0:
            return failure("worker_failed")
        return last
    except KeyboardInterrupt:
        return failure("interrupted")
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return failure("worker_failed")
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if child.poll() is not None:
                for stream in (child.stdin, child.stdout):
                    try:
                        if stream:
                            stream.close()
                    except (OSError, ValueError):
                        pass
            if thread:
                thread.join(timeout=0.1)
            if writer:
                writer.join(timeout=0.1)
            if child.poll() is None:
                return failure("worker_cleanup_failed")


def write_report(path, rendered):
    """Unique temporary file; never follow an existing predictable .tmp link."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".wechat-probe-", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(rendered + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["_worker"]:
        try:
            raw = sys.stdin.read(16001)
            if len(raw) > 16000:
                raise ProbeError("configuration_invalid")
            args = json.loads(raw)
            if not isinstance(args, dict) or set(args) != {"config", "duration", "interval", "prefix"}:
                raise ProbeError("configuration_invalid")
            from .db_probe import run

            def emit(report):
                print(json.dumps(validate_report(report), ensure_ascii=False), flush=True)

            run(**args, emit=emit)
        except Exception as exc:
            print(json.dumps(failure(getattr(exc, "code", "probe_failed"))), flush=True)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locations", type=Path, required=True, help="本机账号路径清单，无密钥")
    parser.add_argument("--targets", type=Path, required=True, help="此前用户指定的两会话提示配置")
    parser.add_argument("--duration", type=int, default=0, help="0仅准备；1–900秒为限时只读观察")
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--prefix", default=None, help="测试消息前缀，默认随机生成")
    parser.add_argument("--output", type=Path, help="只保存脱敏统计JSON")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.locations, args.targets)
        prefix = args.prefix or "XLD-" + secrets.token_hex(4).upper()
        if not 0 <= args.duration <= 900 or not 0.5 <= args.interval <= 5 or not re.fullmatch(
                r"XLD-[A-F0-9]{8}", prefix):
            raise ProbeError("configuration_invalid")
        output = None
        if args.output:
            output = args.output.resolve()
            if (output.suffix.lower() != ".json" or
                    output in {args.locations.resolve(), args.targets.resolve()} or
                    any(output.is_relative_to(p) for p in validate_config(config))):
                raise ProbeError("configuration_invalid")
            output.parent.mkdir(parents=True, exist_ok=True)

        def emit(report):
            rendered = json.dumps(validate_report(report), ensure_ascii=False)
            if output:
                write_report(output, rendered)
            # Publish readiness, then at most every 10 polls and the final state.
            if report["status"] != "observing" or report.get("polls", 0) % 10 == 0:
                print(rendered, flush=True)

        result = isolated(config, duration=args.duration, interval=args.interval, prefix=prefix, emit=emit)
        emit(result)
        return 0 if result["status"] in {"ready", "watch_complete"} else 2
    except (OSError, ValueError, TypeError, KeyError, ProbeError):
        print(json.dumps(failure("configuration_invalid")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
