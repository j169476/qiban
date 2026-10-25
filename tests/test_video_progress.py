from datetime import timedelta

import pytest
from conftest import FakeAdapter
from test_cognition import decision, material

from bili_assistant import video_progress as progress
from bili_assistant.errors import AssistantError
from bili_assistant.scheduler import Scheduler

BVID = "BV1xx411c7mD"


class GapAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.windows = []
        self.empty = False

    async def video_context(self, bvid, page_index=0, detail=False, start_seconds=0, end_seconds=None):
        end = end_seconds or 600
        self.windows.append((start_seconds, end, page_index))
        video = material(bvid)
        video.update(cid="100", page_index=page_index)
        video["coverage"].update(start_seconds=start_seconds, end_seconds=end, duration_seconds=1800, complete=False)
        video["segments"] = [{"start": start_seconds, "end": end, "text": f"第 {start_seconds} 秒的实际材料。"}]
        if self.empty:
            video["coverage"].update(kind="metadata_only", end_seconds=0)
            video["segments"] = []
        return video


class GapScheduler(Scheduler):
    async def decide(self, current, references=()):
        return decision(knowledge="补读所得：" + current["video"]["segments"][0]["text"])


async def setup(role):
    role.adapter = GapAdapter()
    await role.video_context(BVID)
    progress.request(role, BVID, 600, 1800, "后半段如何处理异常？")
    return GapScheduler(role, role.store.state("run")["generation"])


@pytest.mark.asyncio
async def test_gap_continues_in_windows_without_duplicate_video_quota(role):
    scheduler = await setup(role)
    tomorrow = role.clock() + timedelta(days=1)
    role.clock = lambda: tomorrow
    assert await progress.step(scheduler)
    state = progress.status(role, BVID)
    assert state["coverage"]["text_intervals"] == [[0, 1200]]
    assert state["coverage"]["text_gaps"] == [[1200, 1800]]
    assert not state["coverage"]["text_complete"]
    assert await progress.step(scheduler)
    assert not await progress.step(scheduler)
    assert role.adapter.windows == [(0, 600, 0), (600, 1200, 0), (1200, 1800, 0)]
    state = progress.status(role, BVID)
    assert state["coverage"]["text_complete"]
    assert not state["coverage"]["continuous_audio_visual_complete"]
    assert len(role.store.records("knowledge")[0]) == 2
    assert len({r["data"]["source_ref"]["id"] for r in role.store.records("knowledge")[0]}) == 2
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_crash_after_read_resumes_analysis_without_download(role):
    class SimulatedCrash(BaseException):
        pass
    class Interrupted(GapScheduler):
        async def decide(self, *args):
            raise SimulatedCrash()
    await setup(role)
    with pytest.raises(SimulatedCrash):
        await progress.step(Interrupted(role, role.store.state("run")["generation"]))
    assert len(role.adapter.windows) == 2
    assert await progress.step(GapScheduler(role, role.store.state("run")["generation"]))
    assert len(role.adapter.windows) == 2
    assert len(role.store.records("knowledge")[0]) == 1
    assert progress.status(role, BVID)["coverage"]["windows_used"] == 1


@pytest.mark.asyncio
async def test_unavailable_window_stops_without_faking_coverage(role):
    scheduler = await setup(role)
    role.adapter.empty = True
    assert await progress.step(scheduler)
    state = progress.status(role, BVID)
    assert state["coverage"]["text_gaps"] == [[600, 1800]]
    assert state["record"]["data"]["requests"][0]["status"] == "blocked"
    assert not await progress.step(scheduler)
    assert not role.store.records("knowledge")[0]


@pytest.mark.asyncio
async def test_local_source_edit_invalidates_coverage(role):
    await setup(role)
    source = role.store.records("source_snapshots")[0][0]
    source["data"]["segments"][0]["text"] = "人工纠正"
    role.store.write("source_snapshots", source["data"], source["version"], "user", source["data"]["id"])
    state = progress.status(role, BVID)
    assert state["coverage"]["text_intervals"] == [] and state["coverage"]["stale_windows"] == 1


@pytest.mark.asyncio
async def test_pending_continuation_obeys_limit_and_pause(role):
    scheduler = await setup(role)
    config = role.store.read("companion")
    config["data"]["video_gap_max_windows"] = 1
    role.store.write("companion", config["data"], config["version"], "test")
    assert await progress.step(scheduler)
    assert not await progress.step(scheduler)
    assert len(role.adapter.windows) == 2
    assert progress.status(role, BVID)["coverage"]["pending"]
    role.stop(paused=True)
    with pytest.raises(AssistantError):
        await progress.step(scheduler)
    assert len(role.adapter.windows) == 2


@pytest.mark.asyncio
async def test_covered_duplicate_and_invalid_requests(role):
    await setup(role)
    assert progress.request(role, BVID, 0, 400, "重复范围")["already_covered"]
    assert progress.request(role, BVID, 600, 1800, "重复请求")["queued"] == 0
    with pytest.raises(AssistantError):
        progress.request(role, BVID, 1700, 1900, "越界")
    with pytest.raises(AssistantError):
        progress.request(role, BVID, float("nan"), 900, "无效")


@pytest.mark.asyncio
async def test_subtitle_window_respects_start_end_and_character_cap(store, monkeypatch):
    import json

    import httpx
    from bilibili_api import video

    from bili_assistant import credentials, media, video_material
    from bili_assistant.bilibili import Bilibili

    class Video:
        def __init__(self, **kwargs):
            pass
        async def get_info(self):
            return {"aid": 1, "title": "长视频", "owner": {"mid": 8}, "pages": [{"cid": 100, "duration": 1800}]}
        async def get_subtitle(self, **kwargs):
            return {"subtitles": [{"lan": "zh", "subtitle_url": "https://i0.hdslb.com/subtitle.json"}]}
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def stream(self, *args):
            return self
        def raise_for_status(self):
            pass
        async def aiter_bytes(self):
            yield json.dumps({"body": [
                {"from": 0, "to": 30, "content": "开头"},
                {"from": 650, "to": 680, "content": "中段"},
                {"from": 1200, "to": 1250, "content": "尾段"},
            ]}).encode()
    async def nothing(*args, **kwargs):
        pass
    monkeypatch.setattr(video, "Video", Video)
    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: None)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(media, "attach_visuals", nothing)
    monkeypatch.setattr(video_material, "enhance", nothing)
    result = await Bilibili(store).video_context(BVID, start_seconds=600, end_seconds=900)
    assert [s["text"] for s in result["segments"]] == ["中段"]
    assert result["subtitle_window"] == {"start_seconds": 600, "end_seconds": 900.0, "truncated": False}
    assert not result["coverage"]["complete"]
