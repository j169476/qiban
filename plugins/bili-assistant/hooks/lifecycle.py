"""Fast, stdlib-only hook. No network, model, credentials or transcript reads."""

import json
import os
import re
import sqlite3
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
    if event == "SessionStart":
        write(
            runtime / "hooks" / f"{tid}.json",
            json.dumps({"thread_id": tid, "event": event, "at": datetime.now(timezone.utc).isoformat()}),
        )
        return
    db_path = runtime / "state.sqlite3"
    if event != "SessionEnd" or not db_path.exists():
        return
    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
            row = db.execute("SELECT value FROM state WHERE key='run'").fetchone()
        run = json.loads(row[0]) if row else None
        if run and run["owner"] == tid:
            write(runtime / f"stop-{run['generation']}", "host_session_end")
    except sqlite3.Error:
        # Fail closed for this owner only if the runtime DB cannot be read.
        write(
            runtime / "hooks" / f"end-{tid}.json", json.dumps({"at": datetime.now(timezone.utc).isoformat()})
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError):
        # Hook failure remains visible to the host; do not print payloads.
        raise SystemExit(1) from None
