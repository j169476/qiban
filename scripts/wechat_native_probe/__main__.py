"""Run with --inspect first. --send-once explicitly sends one fixed probe to owner_main."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

from companion_core.contracts import stable_key
from companion_wechat.contracts import SendRequest
from companion_wechat.db_cli import load_config
from companion_wechat.db_source import validate_enrollment
from companion_wechat.receipt_client import DatabaseReceiptClient
from companion_wechat.receiver import DatabaseGateway
from companion_wechat.receiver_cli import open_collector_store
from companion_wechat.receiver_transport import SubprocessTransport

from .protocol import PROJECT, ProbeFailure, require, validate_request
from .win32 import Bridge, Windows


async def experiment(bridge, receipts, request, *, timeout=25, settle_seconds=3.0, emit=lambda value: None):
    """One dispatch; callbacks and DB receipts are independent evidence, no send retry."""
    require(type(settle_seconds) in (int, float) and 0 <= settle_seconds <= 5, "invalid_settle_interval")
    started = time.monotonic()
    token = await receipts.begin(request)
    report = {"send_attempts": 0, "native_completed": False, "client_recorded": False,
              "samples": 0, "all_minimized": True, "foreground_unchanged": True,
              "monitor_errors": 0, "bridge_resident": False, "receipt_checks": 0,
              "settle_seconds": settle_seconds,
              "probe_text_sha256": hashlib.sha256(request.text.encode()).hexdigest(),
              "started_at": datetime.now(timezone.utc).isoformat()}
    monitoring = True

    async def observe():
        while monitoring:
            try:
                focus = bridge.w.focus(bridge.pid, bridge.hwnd)
                report["samples"] += 1
                report["all_minimized"] &= focus["minimized"]
                report["foreground_unchanged"] &= (focus["foreground"] == bridge.foreground
                                                    and not focus["target_foreground"])
            except Exception:
                report["monitor_errors"] += 1
            await asyncio.sleep(0.02)

    monitor = asyncio.create_task(observe())
    try:
        # This opt-in experiment uses a new random nonce. The observed server
        # timestamp is about one second behind the host. Capture the REAL head
        # first, then wait before dispatch; do not forge clocks, subtract from
        # watermark.not_before or widen the production historical-message guard.
        settled_at = time.monotonic() + settle_seconds
        while time.monotonic() < settled_at:
            await asyncio.sleep(max(0.001, settled_at - time.monotonic()))
        await asyncio.to_thread(bridge.load)
        await asyncio.to_thread(bridge.inspect, request.target_ref, request.text)
        emit({"stage": "native_inspected", "target": "owner_main", "send_attempts": 0})
        report["send_attempts"] = 1
        report["dispatch_code"] = await asyncio.to_thread(bridge.submit_once)
        report["bridge_resident"] = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            native = await asyncio.to_thread(bridge.status)
            report["native"] = native
            report["native_completed"] = native["completed"] == 1 and native["error"] == 0
            # Receipt handles are consumed on confirmation. Latch positive evidence:
            # a callback may complete after the DB echo, and rechecking its consumed
            # handle would return uncertain and erase the earlier confirmation.
            if not report["client_recorded"]:
                receipt = await receipts.confirm(request, token)
                report["receipt_checks"] += 1
                report["client_recorded"] = receipt.client_recorded
            if (report["native_completed"] and report["client_recorded"] and
                    not any(native[k] for k in ("tasks", "completions", "callbacks"))):
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(1)
        report["success"] = (report.get("dispatch_code") == 0 and report["native_completed"]
                             and report["client_recorded"] and report["all_minimized"]
                             and report["foreground_unchanged"] and report["samples"] >= 2
                             and not report["monitor_errors"]
                             and not any(report.get("native", {}).get(k, 1)
                                         for k in ("tasks", "completions", "callbacks")))
    except Exception as exc:
        report["success"] = False
        # Fixed codes only, not OS messages, IDs, text, paths or exception reprs.
        report["error"] = str(exc) if isinstance(exc, ProbeFailure) else "probe_failed"
        report["bridge_resident"] = bool(bridge.attempted or bridge.pending)
    finally:
        try:
            await asyncio.to_thread(bridge.close)
        except Exception:
            report["cleanup_uncertain"] = True
            report["success"] = False
        monitoring = False
        await monitor
        await receipts.discard(request, token)
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return report


async def run(args):
    with open_collector_store(PROJECT) as (store, logical_account):
        key = "wechat-db-enrollment:" + stable_key("logical-account", logical_account)
        saved = store.state(key)
        require(saved is not None, "existing_enrollment_required")
        enrollment = validate_enrollment(saved["enrollment"])
        windows = Windows()
        pid, born = windows.select(enrollment["self_username"])
        if args.inspect:
            return {"mode": "read_only_inspect", "source_verified": True, "source_pid": pid,
                    "version": windows.api.identity(pid).version, "window_state": windows.api.window_state(pid),
                    "requested_window_owned": windows.owner(args.window) == pid if args.window else None,
                    "send_attempts": 0}
        require(args.check_receiver or args.diagnose_receipt or args.window is not None, "observed_window_required")
        config = load_config(PROJECT / ".dev/wechat/database-locations.json",
                             PROJECT / ".dev/wechat/test-targets-intent.json")
        config["client_pid"] = pid  # In-memory override after source identity matching, no private config rewrite.
        if args.diagnose_receipt:
            report_path = sorted((PROJECT / ".dev/wechat/native-probe-evidence").glob("20*.json"))[-1]
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            if "probe_text_sha256" in previous:
                payload = {"config": config, "enrollment": enrollment, "checkpoint": saved["checkpoint"],
                           "probe": previous}
                env = dict(os.environ)
                env["PYTHONPATH"] = os.pathsep.join([str(PROJECT), str(PROJECT / "src")])
                child = await asyncio.create_subprocess_exec(
                    str(PROJECT / ".dev/wechat-db-venv/Scripts/python.exe"), "-m",
                    "scripts.wechat_native_probe.diagnostic_worker", stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env=env, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    raw, _ = await asyncio.wait_for(child.communicate((json.dumps(payload) + "\n").encode()), 150)
                    diagnosis = json.loads(raw)
                    return {"mode": "read_only_receipt_diagnosis", "send_attempts": 0,
                            "success": diagnosis.get("reader_ok", False), **diagnosis}
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
        receiver = DatabaseGateway(store, config, account=logical_account,
            transport_factory=lambda: SubprocessTransport(python=str(PROJECT / ".dev/wechat-db-venv/Scripts/python.exe")))
        try:
            await receiver.start()
            require(receiver.status()["state"] == "connected" and receiver._saved["enrollment"] == enrollment,
                    "receiver_not_authenticated")
            if args.check_receiver:
                return {"mode": "read_only_receiver_check", "success": True,
                        "version": windows.api.identity(pid).version,
                        "source_verified": True, "bound_targets_verified": True,
                        "worker_connected": receiver.status()["worker_connected"], "send_attempts": 0}
            if args.diagnose_receipt:
                # Read the already authorized pending inbox range without publishing/acknowledging it.
                # This is diagnostic evidence only, never a replacement send watermark.
                data = await receiver._worker.request("read", checkpoint=receiver._saved["checkpoint"], limit=100)
                report_path = sorted((PROJECT / ".dev/wechat/native-probe-evidence").glob("20*.json"))[-1]
                previous = json.loads(report_path.read_text(encoding="utf-8"))
                finished = datetime.fromisoformat(previous["at"]).timestamp()
                matched = [row for row in data["rows"]
                           if row["target_ref"] == enrollment["targets"]["owner_main"]["username"]
                           and row["text"].startswith("XLN_NATIVE_PROBE_")]
                return {"mode": "read_only_receipt_diagnosis", "send_attempts": 0,
                        "success": True, "probe_rows": [{
                            "direction": row["direction"], "sender_is_self": row["sender_ref"] == enrollment["self_username"],
                            "valid_server_id": str(row["server_id"]).isdigit() and int(row["server_id"]) > 0,
                            "seconds_after_probe_report": round(datetime.fromisoformat(row["occurred_at"]).timestamp() - finished, 3),
                            "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
                        } for row in matched], "range_rows": len(data["rows"]),
                        "cursor_persisted": False}
            binding, connection = receiver.bindings.get("owner_main"), receiver.connection()
            request = SendRequest(uuid.uuid4().hex, uuid.uuid4().hex, connection.account_identity, connection.epoch,
                                  "owner_main", binding.revision, binding.target_ref, "XLN_NATIVE_PROBE_" + uuid.uuid4().hex)
            validate_request(request, enrollment, binding)
            focus = windows.focus(pid, args.window)
            require(focus["minimized"] and not focus["target_foreground"], "background_required")
            bridge = Bridge(windows, pid, born, enrollment["self_username"], args.window, focus["foreground"])
            return await experiment(bridge, DatabaseReceiptClient(receiver), request,
                                    emit=lambda value: print(json.dumps(value), flush=True))
        finally:
            await receiver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--check-receiver", action="store_true")
    mode.add_argument("--diagnose-receipt", action="store_true")
    mode.add_argument("--send-once", action="store_true")
    parser.add_argument("--window", type=int, help="Current window ID observed through Sky; never selected by title")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        result = {"success": False, "error": str(exc) if isinstance(exc, ProbeFailure) else "preflight_failed"}
    result["at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.send_once or args.diagnose_receipt or args.check_receiver:
        output = PROJECT / ".dev/wechat/native-probe-evidence"
        output.mkdir(parents=True, exist_ok=True)
        # A metadata-only artifact; never includes wxid, text, pointers, credentials or keys.
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if not args.send_once:
            name = ("diagnosis-" if args.diagnose_receipt else "receiver-") + name
        (output / (name + ".json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.get("success", args.inspect and result.get("source_verified", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
