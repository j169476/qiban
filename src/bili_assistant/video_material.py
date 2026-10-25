"""Bounded Bilibili audio and timeline frames; Codex remains the interpreter."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import AssistantError


def readiness(store):
    settings = store.read("companion")["data"]
    model = Path(settings.get("asr_model_path") or store.root / "models" / "faster-whisper-base")
    return {"ffmpeg": shutil.which("ffmpeg"), "asr_package": importlib.util.find_spec("faster_whisper") is not None,
            "model_ready": (model / "model.bin").is_file() and (model / "config.json").is_file(),
            "model_path": str(model), "mode": settings.get("media_fallback", "auto")}


def stream_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 80, 443) or not any(
                parsed.hostname == domain or parsed.hostname.endswith("." + domain)
                for domain in ("bilivideo.com", "bilivideo.cn", "bilibili.com", "hdslb.com"))):
        raise AssistantError("media_url", "媒体流必须来自 B站播放器返回的 CDN")
    return parsed._replace(scheme="https", netloc=parsed.hostname).geturl()


def choose_streams(data):
    dash = data.get("dash") or {}
    def choose(kind):
        rows = [r for r in dash.get(kind, []) if isinstance(r, dict) and (r.get("baseUrl") or r.get("base_url"))]
        if kind == "video":
            rows = [r for r in rows if str(r.get("codecs", "")).startswith("avc")]
        rows.sort(key=lambda r: float(r.get("bandwidth") or 0))
        return stream_url(rows[0].get("baseUrl") or rows[0]["base_url"]) if rows else None
    audio, video = choose("audio"), choose("video")
    # HTML5 MP4 is a single muxed stream. FLV and multi-file playlists are not interpreted.
    if not audio and not video and "mp4" in str(data.get("format", "")).lower():
        rows = data.get("durl") or []
        if len(rows) == 1:
            audio = video = stream_url(rows[0]["url"])
    return audio, video


async def download(url, path, referer, byte_limit):
    size = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and int(length) > byte_limit:
                raise AssistantError("media_size", "视频/音频流超出本轮下载容量")
            with path.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > byte_limit:
                        raise AssistantError("media_size", "视频/音频流超出本轮下载容量")
                    output.write(chunk)
    return size


async def command(args, timeout):
    process = await asyncio.create_subprocess_exec(
        *map(str, args), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
    try:
        await asyncio.wait_for(process.wait(), timeout)
        if process.returncode:
            raise AssistantError("media_decode", "本地媒体解码或转写未完成")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def scene_changes(ffmpeg, source, start, seconds, threshold, timeout=20):
    """Read bounded FFmpeg metadata from the local file; never fetch a new source."""
    process = await asyncio.create_subprocess_exec(
        *map(str, [ffmpeg, "-nostdin", "-hide_banner", "-v", "info", "-protocol_whitelist", "file",
                   "-ss", start, "-f", "mov", "-i", source, "-t", seconds, "-an", "-sn", "-dn",
                   "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
    )

    async def collect():
        output = bytearray()
        while chunk := await process.stderr.read(16384):
            output.extend(chunk)
            if len(output) > 512_000:
                raise AssistantError("scene_output_limit", "场景检测日志超限，改用时间取样")
        await process.wait()
        if process.returncode:
            raise AssistantError("scene_decode", "场景检测未完成，改用时间取样")
        stamps = set()
        for value in re.findall(rb"pts_time:([-+0-9.eE]+)", output):
            try:
                stamp = start + float(value)
            except ValueError:
                continue
            if math.isfinite(stamp) and start <= stamp < start + seconds:
                stamps.add(stamp)
            if len(stamps) > 4096:
                raise AssistantError("scene_output_limit", "场景候选超限，改用时间取样")
        return sorted(stamps)

    try:
        return await asyncio.wait_for(collect(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def frame_timestamps(start, seconds, count, scenes=()):
    """One candidate per time cell preserves coverage when cuts cluster early."""
    stamps = []
    for index in range(count):
        left, right = start + index * seconds / count, start + (index + 1) * seconds / count
        selected = next((stamp for stamp in scenes if left <= stamp < right), (left + right) / 2)
        stamps.append(selected)
    return stamps


def frame_signature(path):
    from PIL import Image

    with Image.open(path) as picture:
        # A larger fingerprint and max-difference check preserve localized text
        # changes that an average over a tiny thumbnail would wash out.
        return picture.convert("L").resize((64, 36), Image.Resampling.BILINEAR).tobytes()


def similar_frames(left, right):
    if not left or not right or len(left) != len(right):
        return False
    delta = [abs(a - b) for a, b in zip(left, right)]
    return max(delta) < 24 and sum(delta) / len(delta) < 2.5


async def frames(ffmpeg, source, folder, start, seconds, count, *,
                 scene_detection=False, scene_threshold=0.3, dedup=False):
    from PIL import Image, ImageDraw

    if (type(count) is not int or not 1 <= count <= 16 or not math.isfinite(start) or start < 0
            or not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(scene_threshold)
            or not 0.05 <= scene_threshold <= 0.9):
        raise AssistantError("frame_options", "画面取样参数无效")
    clock = asyncio.get_running_loop().time
    deadline = clock() + 60
    sampling = {"method": "uniform", "scene_detection": "disabled", "duplicates_removed": 0,
                "candidates": count, "processed_candidates": 0,
                "window": {"start_seconds": start, "end_seconds": start + seconds},
                "processed_window": {"start_seconds": start, "end_seconds": start}}
    scenes = []
    if scene_detection:
        try:
            scenes = await scene_changes(ffmpeg, source, start, seconds, scene_threshold)
            sampling.update(method="scene_and_time" if scenes else "uniform", scene_detection="completed",
                            scene_candidates=len(scenes))
        except (AssistantError, OSError, TimeoutError) as exc:
            sampling.update(scene_detection="fallback", fallback_reason=getattr(exc, "code", "scene_unavailable"))
    stamps = frame_timestamps(start, seconds, count, scenes)
    thumbs = []
    previous = None
    for index, stamp in enumerate(stamps):
        remaining = deadline - clock()
        if remaining <= 0:
            sampling["truncated"] = True
            break
        target = folder / f"frame-{index}.png"
        try:
            await command([ffmpeg, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file",
                           "-ss", stamp, "-f", "mov", "-i", source, "-frames:v", 1,
                           "-vf", "scale=640:360:force_original_aspect_ratio=decrease", target], min(20, remaining))
            if not target.is_file():
                raise AssistantError("frame_missing", "本轮候选画面未解码成功")
            signature = frame_signature(target)
        except (AssistantError, OSError, TimeoutError) as exc:
            sampling.update(truncated=True, stop_reason=getattr(exc, "code", "frame_unavailable"))
            break
        # Coverage is a prefix of time cells whose representative was actually
        # decoded/compared, including deduplicated frames; never the unvisited tail.
        sampling["processed_candidates"] += 1
        sampling["processed_window"]["end_seconds"] = start + (index + 1) * seconds / count
        if dedup and similar_frames(previous, signature):
            sampling["duplicates_removed"] += 1
            continue
        previous = signature
        thumbs.append((target, stamp))
    output = []
    for offset in range(0, len(thumbs), 4):
        group = thumbs[offset:offset + 4]
        sheet = Image.new("RGB", (1280, 768), "#181818")
        draw = ImageDraw.Draw(sheet)
        for i, (path, stamp) in enumerate(group):
            x, y = i % 2 * 640, i // 2 * 384
            with Image.open(path) as picture:
                sheet.paste(picture.convert("RGB"), (x, y + 24))
            draw.text((x + 8, y + 4), f"{stamp:.2f} s", fill="white")
        target = folder.parent / f"timeline-{uuid.uuid4().hex}.jpg"
        sheet.save(target, quality=85)
        output.append({"path": str(target), "kind": "timeline_sheet", "columns": 2, "rows": 2,
                       "timestamps_seconds": [s for _, s in group], "sampling": dict(sampling)})
    return output


async def enhance(adapter, video, page, context, *, detail=False, start_seconds=0, end_seconds=None):
    settings = adapter.store.read("companion")["data"]
    available = readiness(adapter.store)
    status = {"audio": "not_needed", "frames": "not_needed", "issues": []}
    context["media_details"] = status
    if available["mode"] == "off":
        status.update(audio="disabled", frames="disabled")
        return
    no_text = not context.get("segments")
    need_audio = (no_text or detail) and settings.get("asr_enabled", True)
    need_frames = settings["visuals_enabled"] and (detail or not any(
        image.get("kind") in ("preview_sheet", "timeline_sheet") for image in context.get("images", [])))
    if not (need_audio or need_frames):
        return
    if not available["ffmpeg"]:
        status.update(audio="ffmpeg_missing", frames="ffmpeg_missing")
        return
    if need_audio and not (available["asr_package"] and available["model_ready"]):
        status["audio"] = "asr_not_configured"
        need_audio = False
    if not (need_audio or need_frames):
        return
    duration = float(context["coverage"]["duration_seconds"])
    end = min(duration, end_seconds) if end_seconds is not None else duration
    seconds = min(float(settings.get("media_max_seconds", 600)), max(0, end - start_seconds))
    if not math.isfinite(start_seconds) or start_seconds < 0 or seconds <= 0:
        raise AssistantError("media_window", "补读起点必须在视频实际时长内")
    status.update(start_seconds=start_seconds, end_seconds=start_seconds + seconds, complete=False)
    cache = (adapter.store.root / "cache" / "media").resolve()
    if not cache.is_relative_to(adapter.store.root.resolve()):
        raise AssistantError("invalid_path", "媒体目录超出本地数据目录")
    cache.mkdir(parents=True, exist_ok=True)
    try:
        data = await adapter._read(lambda: video.get_download_url(cid=page["cid"]))
        audio_url, video_url = choose_streams(data)
        if not audio_url and not video_url:
            data = await adapter._read(lambda: video.get_download_url(cid=page["cid"], html5=True))
            audio_url, video_url = choose_streams(data)
        # Only our newly created scratch directory is removed, inside the validated cache root.
        with tempfile.TemporaryDirectory(prefix="decode-", dir=cache) as name:
            folder = Path(name).resolve()
            if not folder.is_relative_to(cache):
                raise AssistantError("invalid_path", "临时媒体目录超出缓存")
            streams = {}
            async def obtain(url):
                if url not in streams:
                    target = folder / f"stream-{len(streams)}.mp4"
                    await asyncio.wait_for(download(url, target, context["url"],
                                                   settings.get("media_max_megabytes", 96) * 1_000_000), 90)
                    streams[url] = target
                return streams[url]
            if need_audio:
                status["audio"] = "unavailable"
                if audio_url:
                    try:
                        stream = await obtain(audio_url)
                        wav, result = folder / "audio.wav", folder / "transcript.json"
                        await command([available["ffmpeg"], "-nostdin", "-v", "error", "-y",
                                       "-protocol_whitelist", "file", "-ss", start_seconds, "-f", "mov",
                                       "-i", stream, "-t", seconds, "-vn", "-ac", 1, "-ar", 16000, wav], 45)
                        await command([sys.executable, "-m", "bili_assistant.media_worker", wav,
                                       available["model_path"], result, "--seconds", seconds,
                                       "--chars", settings["subtitle_char_limit"]],
                                      settings.get("asr_timeout_seconds", 180))
                        transcript = json.loads(result.read_text(encoding="utf-8"))
                        for segment in transcript["segments"]:
                            segment["start"] += start_seconds
                            segment["end"] += start_seconds
                        context["transcript"] = transcript
                        status["audio"] = "transcribed" if transcript["segments"] else "no_speech"
                        if transcript["segments"] and context.get("visual_note"):
                            context["visual_note"] = context["visual_note"].replace("未提供音轨，", "另附机器转写，")
                        if no_text and transcript["segments"]:
                            context.update(segments=transcript["segments"], material_status="transcription",
                                           coverage_note=transcript["note"])
                            context["coverage"].update(kind="transcription", start_seconds=start_seconds,
                                end_seconds=(transcript["segments"][-1]["end"] if transcript["truncated"]
                                             else start_seconds + seconds),
                                complete=start_seconds == 0 and seconds >= duration and not transcript["truncated"])
                    except (AssistantError, TimeoutError, OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
                        status["audio"] = "failed"
                        status["issues"].append(getattr(exc, "code", "audio_processing_failed"))
            if need_frames:
                status["frames"] = "unavailable"
                if video_url:
                    try:
                        stream = await obtain(video_url)
                        images = await frames(available["ffmpeg"], stream, folder, start_seconds, seconds,
                                              settings["visuals_max_images"] * 4,
                                              scene_detection=settings.get("visual_scene_detection", True),
                                              scene_threshold=settings.get("visual_scene_threshold", 0.3),
                                              dedup=settings.get("visual_frame_dedup", True))
                        if images:
                            context["images"] = images
                            context["visual_status"] = "timeline_frames"
                            context["visual_note"] = "实际解码画面按场景变化和时间取样；图片上的秒数是定位信息，不代表连续观看。"
                            status["frames"] = "sampled"
                            status["frame_sampling"] = images[0]["sampling"]
                            status["frame_window"] = images[0]["sampling"]["processed_window"]
                            if context["coverage"]["kind"] in ("metadata_only", "frames"):
                                context["coverage"].update(kind="frames", **status["frame_window"], complete=False)
                                context["material_status"] = "sampled_frames"
                    except (AssistantError, TimeoutError, OSError, ValueError, httpx.HTTPError) as exc:
                        status["frames"] = "failed"
                        status["issues"].append(getattr(exc, "code", "frame_processing_failed"))
    except (AssistantError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError, httpx.HTTPError) as exc:
        status["issues"].append(getattr(exc, "code", "media_unavailable"))
    # Retain bounded ordinary timeline files; never recurse or remove the current output.
    keep = {entry["path"] for entry in context.get("images", [])}
    cached = [p for p in cache.glob("timeline-*.jpg") if p.is_file() and not p.is_symlink()]
    for old in sorted(cached, key=lambda p: p.stat().st_mtime, reverse=True)[128:]:
        if str(old) not in keep:
            old.unlink()
