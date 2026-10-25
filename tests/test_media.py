import sys

import httpx
import pytest

from bili_assistant.errors import AssistantError
from bili_assistant.media import (
    attach_visuals,
    download_image,
    image_url,
    preview_sheets,
    validated_image_paths,
)
from bili_assistant.scheduler import worker_command


@pytest.mark.parametrize("url", [
    "https://example.com/f.jpg", "https://hdslb.com.evil.test/f.jpg",
    "https://user:secret@i0.hdslb.com/f.jpg", "file:///x.jpg", "http://127.0.0.1/f.jpg",
])
def test_image_fetch_rejects_arbitrary_destinations(url):
    with pytest.raises(AssistantError):
        image_url(url)


def test_preview_sheet_sampling_keeps_timestamp_layout():
    snapshot = {
        "img_x_len": 2, "img_y_len": 2,
        "index": [0, *range(0, 120, 10)],
        "image": ["//i0.hdslb.com/0.jpg", "//i0.hdslb.com/1.jpg", "//i0.hdslb.com/2.jpg"],
    }
    sheets = preview_sheets(snapshot, 120, 2)
    assert [sheet["timestamps_seconds"] for sheet in sheets] == [[0, 10, 20, 30], [80, 90, 100, 110]]
    assert sheets[1]["url"].endswith("2.jpg")


def test_real_preview_index_sentinel_does_not_shift_cells():
    snapshot = {
        "img_x_len": 10, "img_y_len": 10,
        "index": [0, 0, 5, 10, 15, 21, 26, 31, 36, 42, 47, 53, 58, 64, 69],
        "image": ["//i0.hdslb.com/snapshot.jpg"],
    }
    stamps = preview_sheets(snapshot, 72, 3)[0]["timestamps_seconds"]
    assert len(stamps) == 14 and stamps[1] == 5 and stamps[-1] == 69


@pytest.mark.asyncio
async def test_download_is_bounded_and_uses_actual_image_format(store, monkeypatch):
    import bili_assistant.media as media

    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"\xff\xd8\xfftest-image"))
    monkeypatch.setattr(media.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))
    path = await download_image(store, "//i0.hdslb.com/snapshot")
    assert path.endswith(".jpg")
    assert validated_image_paths(store, [{"path": path}]) == [path]
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 5)
    with pytest.raises(AssistantError, match="5 MB"):
        await download_image(store, "//i0.hdslb.com/oversize")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(302, b""), (200, b"<html>not an image</html>")])
async def test_redirects_and_non_images_are_not_attached(store, monkeypatch, status, body):
    import bili_assistant.media as media

    client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, content=body, headers={"Location": "https://example.com"})
    )
    monkeypatch.setattr(media.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))
    with pytest.raises((AssistantError, httpx.HTTPError)):
        await download_image(store, "//i0.hdslb.com/image")


@pytest.mark.asyncio
@pytest.mark.parametrize("has_frames", [True, False])
async def test_frames_can_supply_partial_coverage_but_cover_cannot(store, monkeypatch, has_frames):
    import bili_assistant.media as media

    async def downloaded(*args):
        return str(store.root / "cache" / "media" / "example.jpg")

    monkeypatch.setattr(media, "download_image", downloaded)

    class Video:
        async def get_video_snapshot(self, **kwargs):
            assert kwargs == {"cid": 42, "json_index": True, "pvideo": False}
            return {
                "img_x_len": 2, "img_y_len": 1, "index": [0, 10, 40],
                "image": ["//i0.hdslb.com/s.jpg"],
            } if has_frames else {}

    class Adapter:
        async def _read(self, operation):
            return await operation()

    adapter = Adapter()
    adapter.store = store
    context = {"coverage": {
        "kind": "metadata_only", "duration_seconds": 60,
        "start_seconds": 0, "end_seconds": 0, "complete": False,
    }}
    await attach_visuals(adapter, Video(), {"pic": "//i0.hdslb.com/cover.jpg"}, {"cid": 42}, context)
    assert context["coverage"]["kind"] == ("frames" if has_frames else "metadata_only")
    assert not context["coverage"]["complete"]
    assert context["visual_status"] == ("sampled_frames" if has_frames else "cover_only")


def test_worker_attaches_native_images_without_repeated_cli_options(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("bili_assistant.scheduler.shutil.which", lambda _: sys.executable)
    command = worker_command(tmp_path, tmp_path / "out.json", tmp_path / "schema.json", ["one.jpg", "two.jpg"])
    assert command.count("--image") == 2
    assert command.count("--color") == 1
    assert command.index("two.jpg") < command.index("--color")
    assert command[-1] == "-"
