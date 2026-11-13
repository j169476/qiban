"""Trusted incomplete-history reports survive restart and stay conversation scoped."""
from datetime import datetime, timezone

import pytest

from tests_postgres.test_wechat_module import compose
from tests_wechat.test_receiver import STAMP, batch, row, snapshot
from tests_wechat.test_receiver_window import window


@pytest.mark.asyncio
async def test_partial_report_survives_restart_reaches_reply_and_does_not_leak(pg_store):
    initial = snapshot()
    group = row(1, group=True) | {"trigger": "mention", "occurred_at": datetime.fromtimestamp(STAMP - 1, timezone.utc).isoformat()}
    first, middle = batch(initial, group)
    second, end = batch(middle, row(1))
    partial = {"status": "incomplete", "reasons": ["checkpoint_unavailable"]}
    complete = {"status": "complete", "reasons": []}
    env = await compose(pg_store, family=True)
    env.receiver.fixed_windows = True
    try:
        env.core.start()
        env.worker.responses.extend([window(initial, end), {**first, "window_complete": False,
            "history_status": {"family": partial, "owner_main": complete}}])
        assert await env.adapter.collect_once() == []
        assert env.receiver._saved["history"]["family"] == partial
    finally:
        env.core.stop()
        await env.adapter.close()
    env = await compose(pg_store, family=True, previous=middle, authorized=False)
    env.receiver.fixed_windows = True
    try:
        env.core.start()
        # A later page with no additional missing anchors cannot erase the earlier report.
        env.worker.responses.append({**second, "window_complete": True,
            "history_status": {"family": complete, "owner_main": complete}})
        pending = await env.adapter.collect_once()
        assert len(pending) == 2
        old_group = None
        for event in pending:
            batch_record = env.session.merged.for_event(event)
            if event.visibility == "group":
                old_group = batch_record
                assert batch_record["history_status"] == partial
            assert (await env.core.handle(event))["action"]["status"] == "succeeded"
            notice = env.model.calls[-1].get("history_status")
            if event.visibility == "group":
                assert notice["complete"] is False
                assert notice["reasons"][0]["code"] == "checkpoint_unavailable"
                assert "原因未确认" in notice["reasons"][0]["text"]
            else:
                assert notice is None
        assert len(env.driver.sent) == 2
        assert await env.adapter.collect_once() == []
        newer = row(2, group=True) | {"trigger": "mention"}
        third, latest = batch(end, newer)
        env.worker.responses.extend([window(end, latest), {**third, "window_complete": True,
            "history_status": {"family": complete, "owner_main": complete}}])
        pending = await env.adapter.collect_once()
        assert len(pending) == 1
        assert env.session.merged.for_event(pending[0])["history_status"] == complete
        assert pg_store.state(old_group["key"])["history_status"] == partial
    finally:
        env.core.stop()
        await env.adapter.close()
