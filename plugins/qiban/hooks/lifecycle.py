"""Fast, stdlib-only hook. No network, model, credentials or transcript reads."""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    payload = json.load(sys.stdin)
    settings = json.loads((Path(__file__).resolve().parents[1] / "runtime.json").read_text(encoding="utf-8"))
    if Path(payload["cwd"]).resolve() != Path(settings["project"]).resolve():
        return
    tid = payload["session_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tid):
        return
    runtime = Path(settings["data"]) / "runtime"
    event = payload["hook_event_name"]
    if event not in {"SessionStart", "SessionEnd"}:
        return
    # A view/session ending is not an archive or desktop-exit event. The core
    # verifies archive state and the pinned desktop process independently.
    write(
        runtime / "hooks" / f"{tid}.json",
        json.dumps({"thread_id": tid, "event": event, "at": datetime.now(timezone.utc).isoformat()}),
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError):
        # Hook failure remains visible to the host; do not print payloads.
        raise SystemExit(1) from None
