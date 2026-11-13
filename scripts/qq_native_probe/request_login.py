"""Explicit manual QR validation. Run only when the user is ready to scan."""
import argparse
import base64
import json
import time
from pathlib import Path

from companion_qq.native_login import NativeLogin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    directory = args.evidence.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.config.read_text(encoding="utf8"))
    worker = NativeLogin(config, timeout=60)
    report = {"status": "starting", "platform_sends": 0, "model_calls": 0, "message_session_started": False}
    picture = directory / "login-qr.png"

    def status(value):
        # QR content and full account identifiers never enter diagnostic output.
        (directory / "status.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")

    try:
        assert worker.open() == {"status": "ready"}
        worker.request("login_qr")
        deadline = time.monotonic() + 310
        while time.monotonic() < deadline:
            current = worker.request("login_status")
            state = current["status"]
            if state == "qr_ready" and not picture.exists():
                picture.write_bytes(base64.b64decode(current["png_base64"], validate=True))
            status({"status": state})
            if state == "authenticated":
                # Local account output is separate from the shareable result.
                (directory / "account.json").write_text(json.dumps({"uin": current["uin"]}), encoding="utf8")
                report.update(status="authenticated", account_suffix=current["uin"][-4:])
                break
            if state in {"expired", "failed", "cancelled"}:
                report["status"] = state
                break
            time.sleep(.3)
        else:
            report["status"] = "timeout"
        worker.request("close")
        worker.process.wait(5)
        report["normal_exit"] = worker.process.returncode == 0
    except Exception as exc:
        report.update(status="failed", error=getattr(exc, "code", type(exc).__name__))
        raise
    finally:
        worker.close()
        picture.unlink(missing_ok=True)
        report["failure"] = worker.last_failure
        status({"status": report["status"]})
        (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
