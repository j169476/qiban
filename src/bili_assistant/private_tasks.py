"""A private video request reuses the existing quota-aware material reader."""
from copy import copy
from zoneinfo import ZoneInfo

from companion_core.errors import AssistantError
from companion_core.task_workspace import TaskWorkspace

from . import video_progress
from .learning import READ_PROMPT, READ_SCHEMA


async def read_window(connector, task, previous, *, authority, request, guard, generate):
    original, args = connector.engine, task["arguments"]
    workspace = TaskWorkspace(original.core.store, authority, request, task["id"], guard)
    engine = copy(original)
    engine.store = workspace
    settings = workspace.read("companion")["data"]
    if not settings.get("video_gap_enabled", True):
        raise AssistantError("video_gap_disabled", "按缺口补读已关闭")
    past = previous["payload"] if previous else {"intervals": [], "windows": 0, "notes": []}
    missing = video_progress.gaps(past["intervals"], args["start_seconds"], args["end_seconds"])
    if not missing:
        return {"phase": "read", "payload": past, "complete": True, "summary": "请求区间已读。"}
    if past["windows"] >= settings.get("video_gap_max_windows", 6):
        raise AssistantError("video_gap_quota", "本任务已达到补读窗口上限")
    start, end = missing[0]
    end = min(end, start + settings.get("media_max_seconds", 600))
    policy = workspace.read("policy")["data"]
    day = engine.clock().astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
    quota = f"video-gaps:{connector.account}:{day}"
    with original.core.store.lock, original.core.store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        guard()
        used = original.core.store.state(quota, 0, db)
        if used >= settings.get("video_gap_daily_limit", 6):
            raise AssistantError("video_gap_quota", "当日补读额度已用完")
        original.core.store.set_state(quota, used + 1, db)
    video = await engine.video_context(args["bvid"], args["page_index"], detail=args["mode"] == "visual",
        start_seconds=start, end_seconds=end, continuation=bool(previous))
    workspace.check()
    actual = video["coverage"]
    blocked = workspace.read("preferences")["data"]
    if video.get("owner_uid") in blocked["blocked_uids"] or any(
            term.casefold() in video.get("title", "").casefold() for term in blocked["blocked_topics"] if term):
        raise AssistantError("blocked_target", "此视频属于已屏蔽范围")
    if (actual.get("kind") == "metadata_only" or not actual.get("duration_seconds")
            or args["end_seconds"] > actual["duration_seconds"]):
        raise AssistantError("video_material", "视频没有请求范围内可验证的实际材料")
    # Only paths from this trusted platform reader are handed to the isolated worker.
    images = [item["path"] for item in video.get("images", [])[:4]]
    model_video = {key: video[key] for key in ("bvid", "url", "title", "coverage", "segments", "visual_note") if key in video}
    value = await generate(READ_PROMPT, {"objective": args["reason"], "video": model_video}, READ_SCHEMA,
                           images=images, extra_guard=workspace.check)
    workspace.check()
    if not value["knowledge"].strip():
        raise AssistantError("video_material", "本轮材料不足以形成可追溯笔记")
    progress = video["reading_progress"]
    intervals = progress["text_intervals"] if args["mode"] == "text" else progress["visual_sample_intervals"]
    if video_progress.gaps(intervals, start, end) == [[start, end]]:
        raise AssistantError("video_material", "本次没有增加请求区间的实际覆盖")
    payload = {"intervals": video_progress.union([*past["intervals"], *intervals]), "windows": past["windows"] + 1,
        "notes": [*past["notes"], value["knowledge"]], "coverage": actual, "url": video["url"],
        "continuous_audio_visual_complete": False}
    complete = not video_progress.gaps(payload["intervals"], args["start_seconds"], args["end_seconds"])
    return {"phase": "read" if complete else "reading", "payload": payload, "complete": complete,
            "summary": value["knowledge"],
            "source_pins": [(key, version) for key, version in workspace.pins.items()
                            if key.startswith(("source_snapshots/", "task_requests/"))]}
