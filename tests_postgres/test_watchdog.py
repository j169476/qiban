import os
import subprocess
import sys

import pytest

from companion_service.watchdog import ProcessHandle, inspect_parent, read, write


def prepare(store, *, at=10, **extra):
    store.set_state("core-run", {"status": "active", "generation": "synthetic-generation", "owner": "role"})
    write(store.runtime / "service-process.json", {"instance": "one", "at": at, "closed": False, **extra})


def test_process_exit_pauses_once_and_preserves_unknown_send(pg_store):
    prepare(pg_store)
    with pg_store.db() as db:
        db.execute("INSERT INTO core_actions VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "action", "dedupe", "synthetic-generation", "fixture", "bot", "message.reply",
            pg_store.ledger_encode("core_actions.body", {}), "dispatching", None, "2026-10-23T00:00:00Z"))
    assert inspect_parent(pg_store, "one", alive=False, now=11) == "exited"
    assert pg_store.state("core-run")["status"] == "paused"
    fault = pg_store.state("fault:synthetic-generation")
    assert fault["code"] == "daemon_exited" and fault["notification"] == "queued"
    assert inspect_parent(pg_store, "one", alive=False, now=12) == "exited"
    assert pg_store.state("fault:synthetic-generation") == fault
    with pg_store.db() as db:
        assert db.execute("SELECT status FROM core_actions WHERE id='action'").fetchone()[0] == "uncertain"
    assert read(pg_store.runtime / "service-process.json")["instance"] == "one"


@pytest.mark.parametrize(("extra", "alive", "expected"), [
    ({"closed": True}, False, "finished"),
    ({"instance": "new"}, False, "finished"),
    ({"maintenance_until": 500}, True, "maintenance"),
])
def test_old_monitor_normal_close_and_restore_preview_do_not_pause_or_notify(pg_store, extra, alive, expected):
    prepare(pg_store, **extra)
    assert inspect_parent(pg_store, "one", alive=alive, now=200) == expected
    assert pg_store.state("core-run")["status"] == "active"
    assert pg_store.state("fault:synthetic-generation") is None


def test_dead_parent_is_not_excused_by_maintenance_and_stale_heartbeat_does_not_resume(pg_store):
    prepare(pg_store, maintenance_until=500)
    assert inspect_parent(pg_store, "one", alive=False, now=200) == "exited"
    assert pg_store.state("core-run")["status"] == "paused"
    prepare(pg_store)
    assert inspect_parent(pg_store, "one", alive=True, now=200) == "stale"
    assert pg_store.state("core-run")["status"] == "paused"
    assert inspect_parent(pg_store, "one", alive=True, now=11) == "healthy"
    assert pg_store.state("core-run")["status"] == "paused"


@pytest.mark.skipif(os.name != "nt", reason="Windows process object API")
def test_real_process_handle_detects_exit_without_polling_a_reused_pid(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n")
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    handle = ProcessHandle(process.pid)
    try:
        assert handle.born > 0 and handle.alive()
        process.terminate()
        process.wait(timeout=5)
        assert not handle.alive()
    finally:
        handle.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
