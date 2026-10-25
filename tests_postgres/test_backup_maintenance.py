import asyncio
import threading

import pytest

from companion_core.backup_maintenance import BackupMaintenance
from companion_core.postgres_transfer import unpack
from tests_postgres.test_daily_service import started
from tests_postgres.test_service_controls import prepared


def test_backup_runs_while_paused_and_reuses_unchanged_revision(pg_store):
    now = [1000.0]
    worker = BackupMaintenance(pg_store, clock=lambda: now[0])
    first = worker.step()
    path = pg_store.root / "history/backups" / first["filename"]
    assert first["status"] == "saved" and first["raw_bytes"] > 0
    assert unpack(path.read_bytes(), pg_store._backup_key)["tables"]["content_records"]["rows"]
    now[0] += 3700
    pg_store.set_state("service-heartbeat", {"at": now[0]})
    unchanged = worker.step()
    assert unchanged["filename"] == first["filename"] and unchanged["last_saved_at"] == first["last_saved_at"]
    assert unchanged["next_at"] > now[0]
    assert len(list(path.parent.glob("*.ccbackup"))) == 1
    policy = pg_store.read("policy")
    pg_store.write("policy", {**policy["data"], "daily_model_cycles": 22}, policy["version"], "admin-fixture")
    now[0] = unchanged["next_at"] + 1
    second = worker.step()
    assert second["status"] == "saved" and second["filename"] != first["filename"]
    assert pg_store.state("core-run") is None


@pytest.mark.asyncio
async def test_local_conversation_is_in_independent_backup(pg_store):
    service, token = started(pg_store)
    service.local.post(token, "synthetic-private-backup", "a" * 32)
    await service.drain_local()
    service._pause("fixture")
    result = service.backups.step()
    path = pg_store.root / "history/backups" / result["filename"]
    tables = unpack(path.read_bytes(), pg_store._backup_key)["tables"]
    assert len(tables["core_events"]["rows"]) == 1
    assert len(tables["core_actions"]["rows"]) == 1
    assert service.status()["status"] == "paused"


def test_failed_backup_backoff_preserves_previous_and_retries(pg_store, monkeypatch):
    now = [1000.0]
    worker = BackupMaintenance(pg_store, clock=lambda: now[0])
    good = worker.step()
    original = pg_store.backup
    pg_store.set_state("synthetic-new-data", True)
    now[0] += 3700

    def fail(**kwargs):
        raise OSError("PRIVATE-CANARY database password or path")

    monkeypatch.setattr(pg_store, "backup", fail)
    failed = worker.step()
    assert failed["status"] == "failed" and failed["error"] == "backup_failed"
    assert "PRIVATE-CANARY" not in str(worker.status())
    assert worker.step() is None
    assert (pg_store.root / "history/backups" / good["filename"]).is_file()
    monkeypatch.setattr(pg_store, "backup", original)
    now[0] = failed["next_at"] + 1
    assert worker.step()["status"] == "saved"


def test_disabled_backup_does_not_create_files(pg_store):
    policy = pg_store.read("policy")
    pg_store.write("policy", {**policy["data"], "auto_backup": False}, policy["version"], "fixture")
    assert BackupMaintenance(pg_store).step() is None
    assert not list((pg_store.root / "history/backups").glob("*.ccbackup"))


@pytest.mark.asyncio
async def test_backup_does_not_block_heartbeat_and_shutdown_joins_writer(pg_store, monkeypatch):
    service = prepared(pg_store)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(10)
        finished.set()

    monkeypatch.setattr(service.backups, 'step', slow)
    service.task = asyncio.create_task(service.work())
    service.wakeup.set()
    assert await asyncio.to_thread(entered.wait, 5)
    assert service.backup_active
    # The backup gate excludes restore but must not hold the heartbeat's lock.
    assert await asyncio.wait_for(asyncio.to_thread(service.heartbeat), 2)
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(.05)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 5)
    assert finished.is_set() and not service.backup_active
    assert service.status()['status'] == 'paused'


def test_backup_incident_notifies_once_without_starting_role(pg_store):
    from companion_service.faults import record

    service = prepared(pg_store)
    assert not service.status()['generation']
    record(service, 'backup_failed', incident='synthetic-first')
    record(service, 'backup_failed', incident='synthetic-first')
    assert len(pg_store.states('fault:%')) == 1
    record(service, 'backup_failed', incident='synthetic-next')
    assert len(pg_store.states('fault:%')) == 2
    assert service.status()['status'] == 'paused'


@pytest.mark.asyncio
async def test_database_interruption_does_not_kill_monitor_and_recovers_paused(pg_store, monkeypatch):
    service, _ = started(pg_store)
    original = service.heartbeat
    attempts = []

    def pulse():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError('synthetic database outage')
        service.shutdown.set()
        return original()

    monkeypatch.setattr(service, 'heartbeat', pulse)
    await asyncio.wait_for(service.watch(), 7)
    await service.close()
    assert len(attempts) == 2 and service.status()['status'] == 'paused'
    assert next(iter(pg_store.states('fault:%').values()))['code'] == 'storage_unavailable'
