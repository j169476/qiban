from datetime import timedelta

import pytest
from test_cognition import DECISION_SCHEMA, PLAN_SCHEMA, decision

from bili_assistant.api import call
from bili_assistant.discovery import SEARCH_SCHEMA, diversity, expand, observe, search, search_usage
from bili_assistant.errors import AssistantError
from bili_assistant.scheduler import Scheduler


def video(number, owner="888", coverage="subtitles"):
    return {"bvid": f"BV1xx411c{number:03}", "owner_uid": owner, "title": f"视频 {number}",
            "description": "", "url": f"https://www.bilibili.com/video/BV1xx411c{number:03}",
            "coverage": {"kind": coverage}}


def configure(role, **changes):
    record = role.store.read("companion")
    record["data"].update(changes)
    role.store.write("companion", record["data"], record["version"], "test")


def seed(role, count=4):
    for i in range(count):
        observe(role, video(i), "read", "following", ["编程"])


def test_diversity_distinguishes_exposure_reading_and_preference(role):
    configure(role, diversity_min_samples=3)
    for i in range(10):
        observe(role, video(i), "candidate", "following")
    assert not diversity(role)["sample_sufficient"]
    assert diversity(role)["read_samples"] == 0
    observe(role, video(0, coverage="metadata_only"), "read", "following", ["编程"])
    assert diversity(role)["read_samples"] == 0
    seed(role)
    result = diversity(role)
    assert result["counts"]["candidate"] == 10 and result["counts"]["read"] == 4
    assert result["max_topic_share"] == result["max_author_share"] == 1
    assert result["persistent"] and result["classified_samples"] == 4
    assert role.store.read("growth")["data"]["interests"] == {}
    assert not role.store.records("interest_signals")[0]


def test_repeated_video_is_not_an_independent_topic_sample(role):
    configure(role, diversity_min_samples=3)
    seed(role, 3)
    assert not diversity(role)["persistent"]
    for _ in range(8):
        observe(role, video(0), "read", "following", ["编程"])
    result = diversity(role)
    assert result["read_samples"] == 3
    assert not result["persistent"]
    assert result["repeat_read_share"] > 0.7


def test_unclassified_topics_and_window_expiry_are_explicit(role):
    configure(role, diversity_min_samples=3)
    for i in range(5):
        observe(role, video(i, str(100 + i)), "read", "following")
    result = diversity(role)
    assert result["sample_sufficient"] and result["classified_samples"] == 0
    assert not result["topic_concentrated"]
    initial = role.clock()
    role.clock = lambda: initial + timedelta(days=8)
    assert diversity(role)["read_samples"] == 0


def test_guide_metadata_upgrades_to_actual_video_author(role):
    observe(role, {"bvid": video(1)["bvid"]}, "selected", "guide")
    observe(role, video(1, "555"), "read", "guide", ["动画"])
    assert diversity(role)["author_shares"] == {"555": 1}


@pytest.mark.asyncio
async def test_search_quota_shared_and_failure_consumes_attempt(role, admin):
    configure(role, search_daily_limit=1)
    async def fail(query, limit=10):
        raise AssistantError("read_timeout", "timeout")
    role.adapter.search = fail
    with pytest.raises(AssistantError):
        await search(role, "编程")
    assert search_usage(role)["used"] == 1
    with pytest.raises(AssistantError, match="额度"):
        await search(role, "动画")
    assert (await call(admin, "discovery_status", {}))["search_quota"]["remaining"] == 0
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_expansion_cooldown_grounded_queries_and_duplicate_candidates(role, monkeypatch):
    seed(role)
    scheduler = Scheduler(role, role.run()["generation"])
    calls, queries = [], []
    async def model(prompt, payload, schema, images=()):
        calls.append(schema)
        assert schema == SEARCH_SCHEMA
        return {"queries": ["分镜基础", "分镜基础", "互动叙事"], "reason": "尝试编程的相关表达方向"}
    async def query(q, limit=10):
        queries.append(q)
        return [video(10), video(11)]
    role.adapter.search = query
    monkeypatch.setattr(scheduler, "model", model)
    candidates = await expand(scheduler, [], explore=True)
    assert len(candidates) == 2 and all(c["origin"] == "search" for c in candidates)
    assert len(queries) == 2
    await expand(scheduler, [], explore=True)
    assert len(calls) == 1
    assert search_usage(role)["used"] == 2


@pytest.mark.asyncio
async def test_low_model_budget_does_not_plan_unaffordable_search(role, monkeypatch):
    seed(role)
    scheduler = Scheduler(role, role.run()["generation"])
    monkeypatch.setattr(scheduler, "remaining_model_calls", lambda: 2)
    async def forbidden(*args):
        pytest.fail("no model call expected")
    monkeypatch.setattr(scheduler, "model", forbidden)
    assert await expand(scheduler, []) == []


@pytest.mark.asyncio
async def test_blocked_searches_never_reach_platform(role):
    preferences = role.store.read("preferences")
    preferences["data"]["blocked_topics"] = ["赌博"]
    role.store.write("preferences", preferences["data"], preferences["version"], "test")
    with pytest.raises(AssistantError, match="排除"):
        await search(role, "赌博技巧")
    assert search_usage(role)["used"] == 0


@pytest.mark.asyncio
async def test_cycle_searches_then_selects_reads_and_updates_distribution(role, monkeypatch):
    seed(role)
    scheduler = Scheduler(role, role.run()["generation"])
    schemas = []
    async def inbox(since):
        return []
    async def feed(**kwargs):
        return []
    async def query(q, limit=10):
        return [video(10, "777")]
    async def read(bvid, page_index=0):
        result = video(10, "777")
        result["coverage"] = {"kind": "subtitles", "bvid": bvid, "start_seconds": 0,
                              "end_seconds": 20, "duration_seconds": 20, "complete": True}
        return result
    async def model(prompt, payload, schema, images=()):
        schemas.append(schema)
        if schema == SEARCH_SCHEMA:
            return {"queries": ["动画分镜"], "reason": "补充相关方向"}
        if schema == PLAN_SCHEMA:
            return {"bvid": video(10)["bvid"], "reason": "与现有兴趣有关但来自新的作者", "memory_refs": []}
        assert schema == DECISION_SCHEMA
        return decision(video_topics=["动画"], knowledge="读到了字幕里的具体说明")
    role.adapter.inbox, role.adapter.feed = inbox, feed
    role.adapter.search, role.adapter.video_context = query, read
    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert schemas == [SEARCH_SCHEMA, PLAN_SCHEMA, DECISION_SCHEMA]
    result = diversity(role)
    assert result["read_samples"] == 5 and result["topic_shares"]["动画"] == 0.2
    assert search_usage(role)["used"] == 1 and not role.adapter.sent
