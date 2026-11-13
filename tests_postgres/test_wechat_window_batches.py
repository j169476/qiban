"""Real encrypted core storage; synthetic source pages and model/send endpoints."""
from datetime import datetime, timezone

import pytest

from tests_postgres.test_wechat_module import compose
from tests_wechat.test_receiver import STAMP, batch, row, snapshot
from tests_wechat.test_receiver_window import window


@pytest.mark.asyncio
async def test_pages_resume_to_one_reply_and_late_window_gets_separate_batch(pg_store):
    env = await compose(pg_store)
    initial = snapshot()
    first, middle = batch(initial, row(1))
    second, end = batch(middle, row(2))
    frozen = window(initial, end)
    env.receiver.fixed_windows = True
    try:
        env.core.start()
        env.worker.responses.extend([frozen, {**first, "window_complete": False}])
        assert await env.adapter.collect_once() == []
        assert not env.model.calls and not env.driver.sent
        assert len(env.session._record()["admissions"]) == 1
    finally:
        env.core.stop()
        await env.adapter.close()
    env = await compose(pg_store, previous=middle, authorized=False)
    env.receiver.fixed_windows = True
    try:
        env.core.start()
        env.worker.responses.append({**second, "window_complete": True})
        pending = await env.adapter.collect_once()
        assert len(pending) == 1
        frozen_batch = env.session.merged.for_event(pending[0])
        assert len(frozen_batch["members"]) == 2
        assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
        assert len(env.driver.sent) == 1 and len(env.model.calls) == 1
        assert env.model.calls[0]["backlog"]["message_count"] == 2
        assert await env.adapter.collect_once() == []  # Acknowledge final page before next window.
        third, latest = batch(end, row(3))
        env.worker.responses.extend([window(end, latest), {**third, "window_complete": True}])
        pending = await env.adapter.collect_once()
        assert len(pending) == 1
        later = env.session.merged.for_event(pending[0])
        assert later["id"] != frozen_batch["id"] and len(later["members"]) == 1
        assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
        assert len(env.driver.sent) == 2
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [True, False], ids=["group", "private"])
async def test_fixed_range_larger_than_old_admission_cap_drains_without_early_reply(pg_store, group):
    env = await compose(pg_store, family=group)
    env.receiver.fixed_windows = True
    initial = snapshot()
    pages, current = [], initial
    for start in range(1, 2050, 100):
        items = [row(i, group=group, text="bounded synthetic text") for i in range(start, min(start + 100, 2050))]
        for item in items:
            item["occurred_at"] = datetime.fromtimestamp(STAMP - 10, timezone.utc).isoformat()
        if group and start == 1:
            items[0]["trigger"] = "mention"
        data, current = batch(current, *items)
        pages.append(data)
    try:
        env.core.start()
        env.worker.responses.append(window(initial, current))
        for index, data in enumerate(pages):
            env.worker.responses.append({**data, "window_complete": index == len(pages) - 1})
            pending = await env.adapter.collect_once()
            if index != len(pages) - 1:
                assert pending == []
            assert not env.model.calls and not env.driver.sent
        assert len(pending) == 1
        assert len(env.session.merged.for_event(pending[0])["members"]) == 2049
        with pg_store.db() as db:
            assert all(record[0].startswith("enc1:") for record in db.execute("SELECT body FROM core_events"))
    finally:
        env.core.stop()
        await env.adapter.close()
