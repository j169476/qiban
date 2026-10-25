"""Reuse Bilibili preview sheets as native Codex image inputs; no second model stack."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import AssistantError
from .storage import atomic_write

MAX_IMAGE_BYTES = 5_000_000


def image_url(value):
    if not isinstance(value, str):
        raise AssistantError("image_url", "图片地址无效")
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("http://"):
        value = "https://" + value[7:]
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not any(
            parsed.hostname == host or parsed.hostname.endswith("." + host)
            for host in ("hdslb.com", "bilibili.com")
        )
    ):
        raise AssistantError("image_url", "图片必须来自 B站图片域名")
    return value


def image_suffix(raw):
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return ".webp"
    raise AssistantError("image_format", "预览素材不是支持的图片格式")


async def download_image(store, value):
    url = image_url(value)
    folder = (store.root / "cache" / "media").resolve()
    if not folder.is_relative_to(store.root):
        raise AssistantError("invalid_path", "图片缓存超出数据目录")
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()
    for suffix in (".jpg", ".png", ".webp"):
        cached = folder / (key + suffix)
        if cached.is_file() and not cached.is_symlink() and 0 < cached.stat().st_size <= MAX_IMAGE_BYTES:
            cached.touch()
            return str(cached)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_IMAGE_BYTES:
                    raise AssistantError("image_size", "单张图片超过 5 MB")
    target = folder / (key + image_suffix(raw))
    if target.is_symlink():
        raise AssistantError("invalid_path", "图片缓存不接受符号链接")
    atomic_write(target, bytes(raw))
    # Only ordinary files in this resolved cache directory, never a recursive delete.
    cached_files = [
        p for p in folder.iterdir()
        if p.is_file() and not p.is_symlink() and p.suffix in (".jpg", ".png", ".webp")
    ]
    for old in sorted(cached_files, key=lambda p: p.stat().st_mtime, reverse=True)[128:]:
        old.unlink()
    return str(target)


def preview_sheets(snapshot, duration, limit):
    """Keep sheet/time correspondence, including snapshots from a specific multipart CID."""
    columns, rows = int(snapshot.get("img_x_len", 0)), int(snapshot.get("img_y_len", 0))
    times, urls = snapshot.get("index", []), snapshot.get("image", [])
    if not (1 <= columns <= 30 and 1 <= rows <= 30) or not isinstance(times, list):
        return []
    if not isinstance(urls, list) or len(urls) > 100 or len(times) > 50000:
        return []
    # videoshot index[0] is a sentinel; index[1] is the first cell's time.
    if not times or times[0] != 0:
        return []
    times = times[1:]
    per_sheet = columns * rows
    available = min(len(urls), math.ceil(len(times) / per_sheet))
    count = min(available, limit)
    if count < 1:
        return []
    indices = [0] if count == 1 else sorted({round(i * (available - 1) / (count - 1)) for i in range(count)})
    result = []
    for index in indices:
        stamps = times[index * per_sheet : (index + 1) * per_sheet]
        if not stamps or any(
            type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= duration for t in stamps
        ):
            continue
        result.append({
            "url": urls[index], "kind": "preview_sheet",
            "columns": columns, "rows": rows, "timestamps_seconds": stamps,
        })
    return result


async def attach_visuals(adapter, video, info, page, context):
    settings = adapter.store.read("companion")["data"]
    context["images"] = []
    context["visual_status"] = "disabled" if not settings["visuals_enabled"] else "unavailable"
    if not settings["visuals_enabled"]:
        return
    limit = settings["visuals_max_images"]
    candidates = []
    try:
        snapshot = await adapter._read(
            lambda: video.get_video_snapshot(cid=page["cid"], json_index=True, pvideo=False)
        )
        candidates = preview_sheets(snapshot, context["coverage"]["duration_seconds"], limit)
    except (AssistantError, ValueError, TypeError, KeyError, AttributeError):
        pass
    # Prefer actual sampled frames. The cover is supplemental and never proves video coverage.
    if len(candidates) < limit and info.get("pic"):
        candidates.append({"url": info["pic"], "kind": "cover"})
    for candidate in candidates:
        try:
            path = await download_image(adapter.store, candidate["url"])
            context["images"].append({k: v for k, v in candidate.items() if k != "url"} | {"path": path})
        except (AssistantError, httpx.HTTPError, OSError, ValueError):
            continue
    frames = [image for image in context["images"] if image["kind"] == "preview_sheet"]
    if frames:
        context["visual_status"] = "sampled_frames"
        if context["coverage"]["kind"] == "metadata_only":
            stamps = [stamp for image in frames for stamp in image["timestamps_seconds"]]
            context["coverage"].update(
                kind="frames", start_seconds=min(stamps), end_seconds=max(stamps), complete=False
            )
            context["material_status"] = "sampled_frames"
    elif context["images"]:
        context["visual_status"] = "cover_only"
    if context["images"]:
        context["visual_note"] = (
            "图片按 images 顺序提供；preview_sheet 按从左到右、从上到下对应时间表。"
            "预览图是低分辨率稀疏采样，不代表连续观看；未提供音轨，封面不证明视频内容。"
        )


def validated_image_paths(store, images):
    folder = (store.root / "cache" / "media").resolve()
    if not folder.is_relative_to(store.root):
        raise AssistantError("invalid_path", "图片缓存超出数据目录")
    paths = []
    for entry in images[:4]:
        path = Path(entry["path"])
        resolved = path.resolve()
        if (
            not resolved.is_relative_to(folder)
            or path.is_symlink()
            or not resolved.is_file()
            or not 0 < resolved.stat().st_size <= MAX_IMAGE_BYTES
        ):
            raise AssistantError("invalid_image", "只能附带本轮缓存的 B站图片")
        image_suffix(resolved.read_bytes())
        paths.append(str(resolved))
    return paths
