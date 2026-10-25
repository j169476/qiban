"""Local FFmpeg scene sampling; no network, account or model calls."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from bili_assistant import video_material as media
from bili_assistant.errors import AssistantError


def test_scene_selection_keeps_time_cells_and_exact_short_scene_timestamp():
    assert media.frame_timestamps(0, 8, 4, [3.2, 3.4]) == [1, 3.2, 5, 7]
    assert media.frame_timestamps(100, 8, 4, [103.2, 103.4]) == [101, 103.2, 105, 107]


def test_fingerprint_deduplicates_noise_but_preserves_localized_text_change(tmp_path):
    first, second, text = [tmp_path / name for name in ("first.png", "second.png", "text.png")]
    Image.new("RGB", (640, 360), (20, 20, 20)).save(first)
    Image.new("RGB", (640, 360), (21, 21, 21)).save(second)
    changed = Image.new("RGB", (640, 360), (20, 20, 20))
    ImageDraw.Draw(changed).rectangle((100, 100, 140, 120), fill="white")
    changed.save(text)
    assert media.similar_frames(media.frame_signature(first), media.frame_signature(second))
    assert not media.similar_frames(media.frame_signature(first), media.frame_signature(text))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), AssistantError("scene_output_limit", "fake overflow"), OSError()])
async def test_scene_failure_falls_back_to_bounded_uniform_sampling(tmp_path, monkeypatch, failure):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    requested = []

    async def scenes(*args, **kwargs):
        raise failure

    async def command(args, timeout):
        requested.append(float(args[args.index("-ss") + 1]))
        assert timeout <= 20 and "-protocol_whitelist" in args
        Image.new("RGB", (640, 360), "blue").save(Path(args[-1]))

    monkeypatch.setattr(media, "scene_changes", scenes)
    monkeypatch.setattr(media, "command", command)
    result = await media.frames("ffmpeg", tmp_path / "source.mp4", scratch, 10, 8, 4, scene_detection=True, dedup=True)
    assert requested == [11, 13, 15, 17]
    assert len(result) == 1 and result[0]["timestamps_seconds"] == [11]
    assert result[0]["sampling"]["scene_detection"] == "fallback"
    assert result[0]["sampling"]["duplicates_removed"] == 3
    assert result[0]["sampling"]["processed_candidates"] == 4
    assert result[0]["sampling"]["processed_window"] == {"start_seconds": 10, "end_seconds": 18}


@pytest.mark.asyncio
async def test_actual_scene_detection_catches_brief_frame_uniform_grid_misses(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg optional on this runner")
    video = tmp_path / "brief.mp4"
    # A 0.2 s title/step between 3.2 and 3.4 s lies between the old 1/3/5/7 s samples.
    await media.command([
        ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=black:s=160x90:r=25:d=8,drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,3.2,3.39)'",
        "-c:v", "mpeg4", video,
    ], 20)
    scenes = await media.scene_changes(ffmpeg, video, 0, 8, 0.3)
    assert any(3.19 <= stamp <= 3.25 for stamp in scenes)
    scratch = tmp_path / "frames"
    scratch.mkdir()
    images = await media.frames(ffmpeg, video, scratch, 0, 8, 4, scene_detection=True, dedup=True)
    timestamps = [stamp for image in images for stamp in image["timestamps_seconds"]]
    assert any(3.19 <= stamp <= 3.25 for stamp in timestamps)
    assert len(images) <= 1
    assert images[0]["sampling"]["method"] == "scene_and_time"
    assert images[0]["sampling"]["duplicates_removed"] >= 1
    with Image.open(scratch / "frame-1.png") as key_frame:
        assert min(key_frame.convert("RGB").getpixel((100, 100))) > 200
    # Seeking a later window retains absolute source time rather than relabeling its cut as 1.2 s.
    later = await media.scene_changes(ffmpeg, video, 2, 4, 0.3)
    assert any(3.19 <= stamp <= 3.25 for stamp in later)


@pytest.mark.asyncio
async def test_enhancement_keeps_image_quota_and_partial_coverage(store, monkeypatch):
    doc = store.read("companion")
    doc["data"].update(media_fallback="auto", asr_enabled=False, visuals_max_images=1,
                       media_max_seconds=60, visual_scene_detection=True, visual_frame_dedup=True)
    store.write("companion", doc["data"], doc["version"], "test")
    monkeypatch.setattr(media, "readiness", lambda _: {"ffmpeg": "ffmpeg", "mode": "auto"})
    captured = {}

    async def read(op):
        return await op()

    class Video:
        async def get_download_url(self, **kwargs):
            return {"dash": {"video": [{"baseUrl": "https://a.bilivideo.com/video", "codecs": "avc1"}]}}

    async def download(url, target, referer, limit):
        target.write_bytes(b"fixture")

    async def frames(ffmpeg, source, folder, start, seconds, count, **kwargs):
        captured.update(start=start, seconds=seconds, count=count, options=kwargs)
        image = folder.parent / "timeline-test.jpg"
        Image.new("RGB", (640, 360), "white").save(image)
        return [{"path": str(image), "kind": "timeline_sheet", "timestamps_seconds": [start + 5],
                 "sampling": {"method": "scene_and_time", "duplicates_removed": 3,
                              "processed_window": {"start_seconds": start, "end_seconds": start + seconds}}}]

    monkeypatch.setattr(media, "download", download)
    monkeypatch.setattr(media, "frames", frames)
    context = {"url": "https://www.bilibili.com/video/BV1xx411c7mD?p=2", "images": [], "segments": [],
               "coverage": {"kind": "metadata_only", "duration_seconds": 1200, "complete": False}}
    await media.enhance(SimpleNamespace(store=store, _read=read), Video(), {"cid": 7}, context,
                        detail=True, start_seconds=200, end_seconds=300)
    assert captured["start"] == 200 and captured["seconds"] == 60 and captured["count"] == 4
    assert captured["options"]["scene_detection"] is True and captured["options"]["dedup"] is True
    assert len(context["images"]) == 1 and not context["coverage"]["complete"]
    assert context["coverage"]["end_seconds"] == 260
    assert context["media_details"]["frame_sampling"]["method"] == "scene_and_time"


@pytest.mark.asyncio
async def test_scene_log_overflow_reaps_worker_without_unbounded_buffer(tmp_path, monkeypatch):
    class Reader:
        async def read(self, limit):
            return b"x" * limit

    class Process:
        stderr = Reader()
        returncode = None
        killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = -1

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(AssistantError) as error:
        await media.scene_changes("ffmpeg", tmp_path / "source.mp4", 0, 10, 0.3)
    assert error.value.code == "scene_output_limit" and process.killed and process.returncode == -1


@pytest.mark.asyncio
@pytest.mark.parametrize("original_kind", ["metadata_only", "subtitles"])
@pytest.mark.parametrize("stop", ["deadline", "timeout", "missing"])
async def test_partial_frames_leave_unprocessed_tail_available_for_visual_continuation(
        role, monkeypatch, original_kind, stop):
    from bili_assistant import video_progress
    from bili_assistant.knowledge import snapshot

    store = role.store
    settings = store.read("companion")
    settings["data"].update(media_fallback="auto", asr_enabled=False, visuals_max_images=1,
                            visual_scene_detection=False, media_max_seconds=60)
    store.write("companion", settings["data"], settings["version"], "test")
    monkeypatch.setattr(media, "readiness", lambda _: {"ffmpeg": "ffmpeg", "mode": "auto"})

    class Video:
        async def get_download_url(self, **kwargs):
            return {"dash": {"video": [{"baseUrl": "https://a.bilivideo.com/video", "codecs": "avc1"}]}}

    async def read(op):
        return await op()

    async def download(url, target, referer, limit):
        target.write_bytes(b"fixture")

    actual_frames = media.frames
    clock, requested = [0], []

    async def command(args, timeout):
        requested.append(float(args[args.index("-ss") + 1]))
        if len(requested) > 1:
            if stop == "timeout":
                raise TimeoutError()
            if stop == "missing":
                return
        Image.new("RGB", (640, 360), "white").save(Path(args[-1]))
        if stop == "deadline":
            clock[0] += 61

    async def frames(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(media.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: clock[0]))
            return await actual_frames(*args, **kwargs)

    monkeypatch.setattr(media, "download", download)
    monkeypatch.setattr(media, "command", command)
    monkeypatch.setattr(media, "frames", frames)
    bvid = "BV1xx411c7mD"
    context = {"bvid": bvid, "title": "test video", "owner_uid": "888", "cid": "7", "page_index": 0,
               "url": f"https://www.bilibili.com/video/{bvid}", "images": [],
               "segments": [{"start": 0, "end": 600, "text": "full subtitles"}] if original_kind == "subtitles" else [],
               "coverage": {"kind": original_kind, "bvid": bvid, "duration_seconds": 600, "start_seconds": 0,
                            "end_seconds": 600 if original_kind == "subtitles" else 0, "complete": False}}
    await media.enhance(SimpleNamespace(store=store, _read=read), Video(), {"cid": 7}, context,
                        detail=True, start_seconds=0, end_seconds=60)
    assert context["images"][0]["timestamps_seconds"] == [7.5]
    sampling = context["media_details"]["frame_sampling"]
    assert sampling["truncated"] and sampling["processed_candidates"] == 1
    assert context["media_details"]["frame_window"] == {"start_seconds": 0, "end_seconds": 15}
    if original_kind == "metadata_only":
        assert context["coverage"]["end_seconds"] == 15
    else:
        assert context["coverage"]["end_seconds"] == 600
    state = video_progress.capture(role, context, snapshot(role, context))
    assert state["visual_sample_intervals"] == [[0, 15]]
    assert video_progress.request(role, bvid, 30, 60, "inspect the unprocessed tail", mode="visual")["queued"] == 1
    assert not state["continuous_audio_visual_complete"]
