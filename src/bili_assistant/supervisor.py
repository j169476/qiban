"""A launch-scoped monitor: it can stop/report a child, never restart one."""

import asyncio
import subprocess
import sys

from filelock import FileLock, Timeout

from .errors import AssistantError
from .health import HEARTBEAT_SECONDS, STALE_SECONDS, Heartbeat, age, fail, record_fault, start_notifier


def command(engine, operation, generation):
    return [
        sys.executable, "-m", "bili_assistant", "--project", str(engine.host.project),
        "--data", str(engine.store.root), operation, "--generation", generation,
    ]


async def supervise(engine, generation):
    store = engine.store
    lock = FileLock(str(store.runtime / "supervisor.lock"), timeout=45)
    try:
        lock.acquire()
    except Timeout:
        return
    process = None
    try:
        with Heartbeat(store, generation, "supervisor") as heartbeat:
            engine.run(generation)
            with (store.runtime / "daemon.log").open("ab") as log:
                process = subprocess.Popen(
                    command(engine, "daemon", generation),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            heartbeat.pulse(phase="monitoring", child_pid=process.pid)
            while True:
                run = store.state("run", {})
                if run.get("generation") != generation or run.get("status") != "active":
                    break
                if process.poll() is not None:
                    fail(engine, "daemon_exited", generation)
                    break
                engine.run(generation)
                health = store.state(f"health:{generation}")
                # Publishing the first heartbeat may take a little time during startup.
                if age(health or {"at": run.get("launch_requested_at")}) > STALE_SECONDS:
                    fail(engine, "heartbeat_lost", generation)
                    break
                await asyncio.sleep(HEARTBEAT_SECONDS)
    except AssistantError as exc:
        fail(engine, exc.code, generation)
    except Exception:
        fail(engine, "unexpected_daemon_error", generation)
    finally:
        # The child observes the stop marker; kill only the child we actually created.
        if process:
            try:
                await asyncio.to_thread(process.wait, 12)
            except subprocess.TimeoutExpired:
                from .scheduler import terminate_tree

                terminate_tree(process)
        run = store.state("run", {})
        if run.get("generation") == generation and run.get("reason"):
            record_fault(engine, run["reason"], generation)
        lock.release()
    start_notifier(engine)
