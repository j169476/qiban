import pytest
from test_learning import SearchAdapter, goal
from test_native_tools import NativeScheduler, web_response
from test_video_progress import BVID, GapAdapter, GapScheduler

from bili_assistant import learning, native_tools, video_progress
from bili_assistant.scheduler import Scheduler


@pytest.mark.asyncio
async def test_empty_video_search_queues_web_for_existing_goal_and_attaches_note(role):
    saved = goal(role)
    saved = learning.update(role, saved, query="最小案例", phase="search")
    role.adapter = SearchAdapter()
    async def empty_search(*args, **kwargs):
        return []
    role.adapter.search = empty_search
    await learning.step(Scheduler(role, role.run()["generation"]))
    current = role.store.read("learning_goals", saved["data"]["id"])
    assert current["data"]["reason"] == "native_web_pending"
    assert len(role.store.records("learning_goals")[0]) == 1
    waiting = await learning.step(Scheduler(role, role.run()["generation"]))
    assert waiting["waiting"] == "native_web"
    assert await native_tools.step(NativeScheduler(role, web_response()))
    current = role.store.read("learning_goals", saved["data"]["id"])
    assert current["data"]["phase"] == "quiz" and current["data"]["knowledge_refs"]
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_goal_waits_for_video_gap_and_tracks_new_source_versions(role):
    saved = goal(role)
    role.adapter = GapAdapter()
    await role.video_context(BVID)
    video_progress.request(role, BVID, 600, 1800, "补齐例子", goal_id=saved["data"]["id"])
    scheduler = GapScheduler(role, role.run()["generation"])
    assert (await learning.step(scheduler))["waiting"] == "video_gap"
    assert await video_progress.step(scheduler)
    current = role.store.read("learning_goals", saved["data"]["id"])
    assert len(current["data"]["knowledge_refs"]) == 1
    assert (await learning.step(scheduler))["waiting"] == "video_gap"
    assert await video_progress.step(scheduler)
    current = role.store.read("learning_goals", saved["data"]["id"])
    assert len(current["data"]["knowledge_refs"]) == 2 and current["data"]["phase"] == "quiz"
    assert not video_progress.status(role, BVID)["coverage"]["pending"]


@pytest.mark.asyncio
async def test_changed_goal_request_discards_native_result(role):
    saved = goal(role)
    native_tools.create(role, "web", "查缺口", goal_id=saved["data"]["id"])
    role.store.path("conversation_turns", saved["data"]["request"]["id"]).unlink()
    scheduler = NativeScheduler(role, web_response())
    assert await native_tools.step(scheduler)
    assert not scheduler.calls and not role.store.records("knowledge")[0]
