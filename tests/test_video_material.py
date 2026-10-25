import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_assistant import video_material as media
from bili_assistant.errors import AssistantError
from bili_assistant.media import validated_image_paths


def settings(store, **changes):
    doc = store.read("companion")
    doc["data"].update(media_fallback="auto", **changes)
    store.write("companion", doc["data"], doc["version"], "test")


def context():
    return {"bvid": "BV1xx411c7mD", "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "segments": [], "images": [], "coverage": {"kind": "metadata_only", "bvid": "BV1xx411c7mD",
            "start_seconds": 0, "end_seconds": 0, "duration_seconds": 1200, "complete": False}}


def test_stream_selection_uses_bounded_low_bandwidth_and_rejects_external_hosts():
    audio, video = media.choose_streams({"dash": {
        "audio": [{"baseUrl": "https://a.bilivideo.com/audio", "bandwidth": 200}],
        "video": [{"baseUrl": "https://a.bilivideo.com/high", "codecs": "avc", "bandwidth": 1000},
                  {"baseUrl": "https://a.bilivideo.com/low", "codecs": "avc", "bandwidth": 100}]}})
    assert audio.endswith("audio") and video.endswith("low")
    for url in ("file:///secret", "https://bilibili.com.evil.test/a", "http://127.0.0.1/a"):
        with pytest.raises(AssistantError):
            media.stream_url(url)


@pytest.mark.asyncio
async def test_transcription_fallback_preserves_partial_time_coverage(store, monkeypatch):
    settings(store, visuals_enabled=False, media_max_seconds=60)
    monkeypatch.setattr(media, "readiness", lambda _: {"ffmpeg": "ffmpeg", "asr_package": True,
                        "model_ready": True, "model_path": "model", "mode": "auto"})
    async def read(op):
        return await op()
    class Video:
        async def get_download_url(self, **kwargs):
            return {"dash": {"audio": [{"baseUrl": "https://a.bilivideo.com/audio"}]}}
    async def download(url, target, referer, limit):
        target.write_bytes(b"fixture")
        return 7
    async def command(args, timeout):
        if "bili_assistant.media_worker" in args:
            Path(args[5]).write_text(json.dumps({"segments": [{"start": 1, "end": 4, "text": "片段转写"}],
                                               "truncated": False, "note": "机器转写"}), encoding="utf-8")
    monkeypatch.setattr(media, "download", download)
    monkeypatch.setattr(media, "command", command)
    data = context()
    await media.enhance(SimpleNamespace(store=store, _read=read), Video(), {"cid": 1}, data, start_seconds=120)
    assert data["segments"][0]["start"] == 121
    assert data["coverage"]["kind"] == "transcription" and not data["coverage"]["complete"]
    assert data["coverage"]["end_seconds"] == 180 and data["media_details"]["audio"] == "transcribed"
    assert not list((store.root / "cache" / "media").glob("decode-*"))


@pytest.mark.asyncio
async def test_missing_asr_does_not_claim_audio_was_read_or_download_weights(store, monkeypatch):
    settings(store, visuals_enabled=False)
    monkeypatch.setattr(media, "readiness", lambda _: {"ffmpeg": "ffmpeg", "asr_package": False,
                        "model_ready": False, "model_path": "model", "mode": "auto"})
    data = context()
    await media.enhance(SimpleNamespace(store=store), object(), {"cid": 1}, data)
    assert data["media_details"]["audio"] == "asr_not_configured" and not data["segments"]
    assert data["coverage"]["kind"] == "metadata_only"


@pytest.mark.asyncio
async def test_actual_ffmpeg_timeline_labels_and_native_image_paths(store, tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg optional on this runner")
    cache = store.root / "cache" / "media"
    cache.mkdir(parents=True)
    scratch = cache / "test-frames"
    scratch.mkdir()
    video = tmp_path / "fixture.mp4"
    await media.command([ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                         "color=c=blue:s=160x90:d=4", "-c:v", "mpeg4", video], 20)
    images = await media.frames(ffmpeg, video, scratch, 1, 2, 4)
    assert len(images) == 1 and images[0]["timestamps_seconds"] == [1.25, 1.75, 2.25, 2.75]
    assert validated_image_paths(store, images) == [images[0]["path"]]
