import base64
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import chain

import pytest

from companion_core import backup_stream, events, history_retention
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from qiban.desktop_notifications import DesktopNotifications


def test_uncertain_send_counts_towards_intake_limit_until_resolved(pg_store, monkeypatch):
    monkeypatch.setattr(history_retention,'HISTORY_BYTES',4096)
    with pg_store.db() as db:
        db.execute("""INSERT INTO actions(id,dedupe,owner,generation,account,kind,payload,status,units,created)
                   VALUES('a','d','system','g','account','like',?,'uncertain',1,'2020-01-01T00:00:00Z')""",
                   (pg_store.ledger_encode('actions.payload',{'text':'x'*8192}),))
    history_retention.cleanup(pg_store,capacity=4096,limit=5000)
    assert history_retention.intake_paused(pg_store)
    with pg_store.db() as db:
        assert not db.execute("SELECT history_pruned FROM actions WHERE id='a'").fetchone()[0]
        db.execute("UPDATE actions SET status='succeeded' WHERE id='a'")
    history_retention.cleanup(pg_store,capacity=4096,limit=5000)
    assert not history_retention.intake_paused(pg_store)
    with pg_store.db() as db:
        assert db.execute("SELECT history_pruned FROM actions WHERE dedupe='d'").fetchone()[0]


def test_oldest_first_and_age_without_losing_receipts(pg_store):
    now = datetime.now(timezone.utc)

    def event_at(age, native):
        event = Event(
            "qq",
            "fixture",
            native,
            ActorRef("qq", "sender"),
            "private:sender",
            (now - timedelta(days=age)).isoformat(),
            "ordinary text",
        )
        version = events.ingest(pg_store, event)["version"]
        return event, version

    old, version = event_at(61, "handled")
    pinned, pinned_version = event_at(80, "evidence")
    pending, _ = event_at(62, "pending")
    recent, recent_version = event_at(1, "recent")
    pg_store.set_state("core-processed:" + old.id, version)
    pg_store.set_state("core-processed:" + recent.id, recent_version)
    pg_store.set_state("core-processed:" + pinned.id, pinned_version)
    pg_store.set_state("action-provenance:fixture", {"pins": [["event/" + pinned.id, pinned_version]]})
    history_retention.cleanup(pg_store, now=now)
    with pytest.raises(AssistantError, match="事件不存在"):
        events.get(pg_store, old.id)
    assert pg_store.state("core-processed:" + old.id) == version
    assert events.get(pg_store, pending.id) and events.get(pg_store, recent.id)
    history_retention.cleanup(pg_store, now=now, capacity=1, limit=5000)
    with pytest.raises(AssistantError):
        events.get(pg_store, recent.id)
    assert events.get(pg_store, pending.id) and events.get(pg_store, pinned.id)
    events.ingest(pg_store, old)
    assert not any(
        e.id == old.id for e in events.pending(pg_store, [("qq", "fixture")], old.occurred_at, limit=100)
    )


def test_chunks_restore_and_rollback(pg_store, tmp_path):
    key = pg_store._backup_key
    pg_store.set_state("fixture-stream", "before" * 50000)
    path = tmp_path / "local.ccbackup"
    archive = backup_stream.create(pg_store, path, key)
    pg_store.set_state("fixture-stream", "after")
    expected = backup_stream.fingerprint(pg_store)
    with pytest.raises(AssistantError):
        backup_stream.restore(pg_store, path, key, expected=expected, digest="invalid-digest")
    assert pg_store.state("fixture-stream") == "after" and backup_stream.fingerprint(pg_store) == expected
    result = backup_stream.restore(pg_store, path, key, expected=expected, digest=archive["sha256"])
    assert result["restored"] and pg_store.state("fixture-stream") == "before" * 50000
    with path.open("r+b") as handle:
        handle.seek(-1, 2)
        handle.truncate()
    with pytest.raises(AssistantError):
        backup_stream.inspect(path, key)


def test_backup_allows_live_writes_and_restores_one_snapshot(pg_store, monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    write = backup_stream.write
    def wait_after_snapshot(stream, entries, key):
        first = next(entries)
        entered.set()
        assert resume.wait(10)
        return write(stream, chain([first], entries), key)
    monkeypatch.setattr(backup_stream, 'write', wait_after_snapshot)
    pg_store.set_state('concurrent-backup-value','before')
    with ThreadPoolExecutor() as pool:
        future = pool.submit(pg_store.backup, force=True)
        try:
            assert entered.wait(10)
            with pg_store.lock.acquire(timeout=2):
                pg_store.set_state('concurrent-backup-value','after')
        finally:
            resume.set()
        archive = future.result()
    from pathlib import Path
    backup_stream.restore(pg_store, Path(archive['path']), pg_store._backup_key,
                          expected=backup_stream.fingerprint(pg_store), digest=archive['sha256'])
    assert pg_store.state('concurrent-backup-value') == 'before'


def test_notice_recurrence_and_90_day_read_retention(pg_store):
    from types import SimpleNamespace

    service = DesktopNotifications(SimpleNamespace(store=pg_store))
    first = service.record("network_error", "run1", "qq")
    second = service.record("network_error", "run2", "qq")
    assert service.record("network_error", "run2", "qq") == second
    with pg_store.db() as db:
        assert service._counts(db) == {"total": 1, "unread": 1}
        assert pg_store.state("desktop-notice:" + first, db=db) is None
        item = pg_store.state("desktop-notice:" + second, db=db)
        assert item["occurrences"] == 2
        pg_store.set_state(
            "desktop-notice:" + second, {**item, "read": True, "read_at": "2020-01-01T00:00:00+00:00"}, db
        )
        pg_store.set_state("desktop-inbox-counts", {"total": 1, "unread": 0}, db)
    service.cleanup()
    with pg_store.db() as db:
        assert service._counts(db) == {"total": 0, "unread": 0}


@pytest.mark.skipif(os.getenv("QIBAN_LARGE_BACKUP_TEST") != "1", reason="Uses a disposable multi-GB database")
def test_physical_backup_over_512_mib_restores(pg_store, tmp_path, request):
    count = 750
    prefix = "fixture-incompressible:"
    payload = base64.b64encode(os.urandom(768 * 1024)).decode("ascii")
    with pg_store.db() as db:
        for sequence in range(count):
            pg_store.set_state(prefix + str(sequence), payload, db)
    target = tmp_path / "large.ccbackup"
    request.addfinalizer(lambda: target.unlink(missing_ok=True))
    created = backup_stream.create(pg_store, target, pg_store._backup_key)
    assert target.stat().st_size > 512 * 1024 * 1024
    pg_store.set_state(prefix + "0", "replaced since backup")
    expected = backup_stream.fingerprint(pg_store)
    backup_stream.restore(pg_store, target, pg_store._backup_key, expected=expected, digest=created["sha256"])
    with pg_store.db() as db:
        with db.stream("SELECT key,value FROM state WHERE key LIKE ?", (prefix + "%",)) as rows:
            seen = 0
            for key, value in rows:
                assert pg_store.ledger_decode("state:" + key, value) == payload
                seen += 1
    assert seen == count
