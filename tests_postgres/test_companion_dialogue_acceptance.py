"""Manual real-model acceptance, exclusively inside a fresh synthetic PostgreSQL schema."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from companion_codex.model import CodexModel
from companion_core import events
from companion_core.contracts import Event
from tests_postgres.test_daily_service import started


@pytest.mark.skipif(os.getenv("QIBAN_LIVE_DIALOGUE") != "1", reason="explicit synthetic live-model acceptance")
@pytest.mark.asyncio
async def test_corrected_name_preference_plan_and_unknown(pg_store, monkeypatch):
    from companion_service import local_adapter

    service, token = started(pg_store, daily_local_replies=40)
    service.core.decisions.model = CodexModel()
    cfg = pg_store.read("companion")
    pg_store.write("companion", {**cfg["data"], "conversation_context_turns": 4}, cfg["version"], "fixture")
    today = datetime.now(timezone.utc)
    class MessageClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return message_time.astimezone(tz)
    monkeypatch.setattr(local_adapter, "datetime", MessageClock)
    messages = (
        "请叫我灰雀。我住在石湾。我喜欢喝淡茶。我们约好周六去看灯展。",
        "以后叫我青禾。我已经搬到柳湾。周六的灯展取消了。",
        "我累的时候希望你先听我说。",
        "折腾了一整天，感觉电都耗光了。",
        "你还记得怎么称呼我、我目前住哪儿、平时爱喝什么、周六那件事还去不去吗？",
        "我上次告诉过你我家门牌号码是多少吗？",
    )
    replies = []
    for message, days in zip(messages, (6, 4, 3, 1, 0, 0), strict=True):
        message_time = today - timedelta(days=days) + timedelta(seconds=len(replies))
        receipt = service.local.post(token, message, uuid4().hex)
        event = Event.from_dict(events.get(pg_store, receipt["id"])["event"])
        await service.core.handle(event)
        turns = pg_store.records("conversation_turns")[0]
        sent = [r["data"] for r in turns if r["data"]["speaker"] == "assistant" and r["data"]["status"] == "sent"]
        assert len(sent) == len(replies) + 1
        replies.append(max(sent, key=lambda r: (r["occurred_at"], r["sequence"]))["content"])
        print("合成回复", len(replies), replies[-1], flush=True)
    assert all(term in replies[-2] for term in ("青禾", "柳湾", "茶"))
    assert any(term in replies[-2] for term in ("取消", "不去", "不去了", "作罢"))
    assert not any(c.isdigit() for c in replies[-1])
    assert any(term in replies[-1] for term in ("没", "不清楚", "不记得", "不知道", "没有"))
