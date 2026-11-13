"""Admission pages commit with their root and survive restart without model calls."""
from datetime import datetime, timezone

import pytest

from tests_postgres.test_wechat_module import compose
from tests_wechat.test_receiver import STAMP, batch, row, snapshot
from tests_wechat.test_receiver_window import window


@pytest.mark.asyncio
async def test_paged_admission_failure_rolls_back_and_restart_restores_order(pg_store, monkeypatch):
    env = await compose(pg_store)
    env.receiver.fixed_windows = True
    initial = snapshot()
    items = [row(i) for i in range(1, 302)]
    for item in items:
        item["occurred_at"] = datetime.fromtimestamp(STAMP - 1, timezone.utc).isoformat()
    first, middle = batch(initial, *items[:100])
    second, after = batch(middle, *items[100:200])
    _, end = batch(after, *items[200:])
    try:
        env.core.start()
        env.worker.responses.extend([window(initial, end), {**first, "window_complete": False},
                                     {**second, "window_complete": False}])
        assert await env.adapter.collect_once() == []
        assert await env.adapter.collect_once() == []
        key = env.session.key
        original = env.session._record()
        root = pg_store.state(key)
        assert root["admission_pages"] == 2 and root["admission_count"] == 200
        saved_page = pg_store.state(key + ":admissions:0")
        save = pg_store.set_state

        def fail_second_page(name, value, db=None):
            if name == key + ":admissions:1":
                raise RuntimeError("synthetic interrupted page write")
            return save(name, value, db)

        monkeypatch.setattr(pg_store, "set_state", fail_second_page)
        with pytest.raises(RuntimeError, match="synthetic interrupted"):
            with pg_store.lock, pg_store.db() as db:
                changed = env.session._record(db)
                changed["admissions"][changed["pending_order"][0]]["ticket"]["expires"] += 1
                env.session._save_record(changed, db)
        monkeypatch.setattr(pg_store, "set_state", save)
        assert pg_store.state(key) == root
        assert pg_store.state(key + ":admissions:0") == saved_page
        assert env.session._record() == original
    finally:
        env.core.stop()
        await env.adapter.close()
    resumed = await compose(pg_store, previous=after, authorized=False)
    resumed.receiver.fixed_windows = True
    try:
        resumed.core.start()
        record = resumed.session._record()
        assert record["pending_order"] == original["pending_order"]
        assert set(record["admissions"]) == set(original["admissions"])
        assert not resumed.model.calls and not resumed.driver.sent
        # Retired inputs shrink the root back to inline storage and remove old pages.
        with pg_store.lock, pg_store.db() as db:
            for event_id in record["pending_order"][:150]:
                pg_store.set_state("merged-consumed:" + event_id,
                    {"version": record["admissions"][event_id]["version"]}, db)
            resumed.session._prune(record, db)
            resumed.session._save_record(record, db)
        assert len(resumed.session._record()["admissions"]) == 50
        assert pg_store.state(key + ":admissions:0") is None
        assert pg_store.state(key + ":admissions:1") is None
    finally:
        resumed.core.stop()
        await resumed.adapter.close()
