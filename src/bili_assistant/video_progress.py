"""Persistent, source-versioned reading windows and demand-driven continuation."""

import math
from zoneinfo import ZoneInfo

from .cognition import save_learning, stable_id, validate_decision, verify_versions
from .errors import AssistantError
from .schema import CONFIG


def key(engine, bvid, page_index):
    account = engine.store.read("manifest")["data"]["account_uid"]
    scope = getattr(engine.store, "scope_key", "")
    return stable_id("video", f"{account}:{bvid}:{page_index}" + (":" + scope if scope else ""))


def read(engine, bvid, page_index=0):
    return engine.store.read("video_progress", key(engine, bvid, page_index))


def union(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1] + 0.01:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def gaps(intervals, start, end):
    result, cursor = [], start
    for left, right in union(intervals):
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            result.append([cursor, min(left, end)])
        cursor = max(cursor, min(right, end))
    if cursor < end:
        result.append([cursor, end])
    return result


def coverage(store, record):
    doc = record["data"]
    windows, stale = {"text": [], "visual": []}, 0
    for window in doc["windows"]:
        try:
            source = store.read("source_snapshots", window["source"]["id"])
            valid = source["version"] == window["source"]["version"]
        except AssistantError:
            valid = False
        if valid:
            windows[window["mode"]].append([window["start_seconds"], window["end_seconds"]])
        else:
            stale += 1
    missing = gaps(windows["text"], 0, doc["duration_seconds"])
    return {"text_intervals": union(windows["text"]), "text_gaps": missing,
            "visual_sample_intervals": union(windows["visual"]), "stale_windows": stale,
            "text_complete": doc["duration_seconds"] > 0 and not missing,
            "continuous_audio_visual_complete": False,
            "pending": [r for r in doc["requests"] if r["status"] in ("pending", "reading")],
            "windows_used": doc["windows_used"], "duration_seconds": doc["duration_seconds"]}


def capture(engine, video, source):
    store = engine.store
    if not video.get("coverage", {}).get("duration_seconds"):
        return {"available": False, "reason": "duration_unknown", "continuous_audio_visual_complete": False}
    page = video.get("page_index", 0)
    item_id = key(engine, video["bvid"], page)
    with store.lock:
        try:
            old = read(engine, video["bvid"], page)
        except AssistantError as exc:
            if exc.code != "not_found":
                raise
            old = None
        duration, cid = video["coverage"]["duration_seconds"], str(video.get("cid", ""))
        reset = old and (old["data"]["duration_seconds"] != duration or old["data"]["cid"] != cid)
        run = engine.run(check_host=False)
        doc = old["data"] if old and not reset else {
            "schema_version": 1, "id": item_id, "account_uid": run["account"], "owner": run["owner"],
            "bvid": video["bvid"], "page_index": page, "cid": cid, "duration_seconds": duration,
            "title": video.get("title", ""), "owner_uid": video.get("owner_uid", ""),
            "windows": [], "requests": [], "windows_used": 0,
            "source": {"type": "video", "recorded_at": engine.clock().isoformat(), "reference": video["url"]},
            "locked": False,
        }
        if doc["locked"]:
            return coverage(store, old)
        if source:
            actual = video["coverage"]
            intervals = []
            if actual["kind"] in ("subtitles", "transcription"):
                window = video.get("subtitle_window", actual)
                start = 0 if actual["complete"] else window["start_seconds"]
                end = duration if actual["complete"] else window["end_seconds"]
                intervals.append(("text", start, min(end, duration)))
            details = video.get("media_details", {})
            if actual["kind"] == "frames":
                intervals.append(("visual", actual["start_seconds"], actual["end_seconds"]))
            elif details.get("frames") == "sampled":
                window = details.get("frame_window", details)
                intervals.append(("visual", window["start_seconds"], window["end_seconds"]))
            for mode, start, end in intervals:
                window = {"source": {"id": source["data"]["id"], "version": source["version"]},
                          "start_seconds": start, "end_seconds": end, "mode": mode}
                if window not in doc["windows"] and end > start:
                    doc["windows"].append(window)
            doc["windows"] = doc["windows"][-100:]
        saved = store.write("video_progress", doc, old["version"] if old else None, engine.identity(), item_id)
    return coverage(store, saved)


def request(engine, bvid, start_seconds, end_seconds, reason, mode="text", page_index=0, goal_id=""):
    run = engine.role_run()
    store = engine.store
    if not store.read("companion")["data"].get("video_gap_enabled", True):
        raise AssistantError("video_gap_disabled", "按缺口补读已关闭")
    goal_ref = None
    if goal_id:
        from .learning import status

        goal = status(engine, goal_id)["goals"][0]
        if goal["data"]["status"] != "active" or not goal["request_current"]:
            raise AssistantError("goal_inactive", "目标已暂停或要求已变化")
        goal_ref = {"id": goal_id, "version": goal["version"]}
    with store.lock:
        old = read(engine, bvid, page_index)
        doc = old["data"]
        if (not all(math.isfinite(v) for v in (start_seconds, end_seconds)) or start_seconds < 0
                or end_seconds <= start_seconds or end_seconds > doc["duration_seconds"]
                or mode not in ("text", "visual") or not reason.strip()):
            raise AssistantError("media_window", "补读需要实际视频范围内的起止时间、模态和具体缺口")
        state = coverage(store, old)
        known = state["text_intervals"] if mode == "text" else state["visual_sample_intervals"]
        missing = gaps(known, start_seconds, end_seconds)
        if not missing:
            return {"already_covered": True, "progress": state}
        if doc["owner"] != run["owner"]:
            doc["owner"] = run["owner"]
        count = 0
        for start, end in missing:
            request_id = stable_id("gap", f"{mode}:{start}:{end}:{goal_id}")
            if any(r["id"] == request_id for r in doc["requests"]):
                continue
            if len(doc["requests"]) >= 60:
                raise AssistantError("video_gap_queue", "本视频补读任务已达上限，请整理本地记录")
            doc["requests"].append({"id": request_id, "mode": mode, "start_seconds": start, "end_seconds": end,
                                    "reason": reason, "status": "pending", "error": "", "goal": goal_ref})
            count += 1
        saved = store.write("video_progress", doc, old["version"], engine.identity(), doc["id"])
    return {"queued": count, "progress": coverage(store, saved), "record": saved}


def from_decision(engine, material, decision):
    gap = decision.get("video_gap")
    if not gap:
        return
    video = material.get("video")
    if not video:
        raise AssistantError("ungrounded_target", "没有本轮视频，不能请求补读")
    try:
        return request(engine, video["bvid"], page_index=video.get("page_index", 0),
                       goal_id=material.get("learning_goal", ""), **gap)
    except AssistantError as exc:
        if exc.code not in ("video_gap_disabled", "video_gap_queue"):
            raise
        engine.store.audit("video", {"event": "gap_skipped", "reason": exc.code})


def status(engine, bvid, page_index=0):
    engine.mode()
    record = read(engine, bvid, page_index)
    return {"record": record, "coverage": coverage(engine.store, record)}


async def step(scheduler):
    engine, store = scheduler.engine, scheduler.engine.store
    run = scheduler.alive()
    settings = store.read("companion")["data"]
    if not settings.get("video_gap_enabled", True) or not scheduler.remaining_model_calls():
        return False
    policy = store.read("policy")["data"]
    day = engine.clock().astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
    daily = f"video-gaps:{run['account']}:{day}"
    for old in store.records("video_progress")[0]:
        doc = old["data"]
        if doc["account_uid"] != run["account"] or doc["owner"] != run["owner"] or doc["locked"]:
            continue
        pending = next((r for r in doc["requests"] if r["status"] in ("pending", "reading")), None)
        if not pending:
            continue
        from .knowledge import ref

        previous = coverage(store, old)
        cached = store.state(f"context:{doc['bvid']}", {})
        resume = (pending["status"] == "reading" and cached.get("gap_request") == pending["id"]
                  and cached.get("page_index", 0) == doc["page_index"] and not previous["stale_windows"])
        if not resume and (doc["windows_used"] >= settings.get("video_gap_max_windows", 6)
                           or store.state(daily, 0) >= settings.get("video_gap_daily_limit", 6)):
            continue
        known = previous["text_intervals"] if pending["mode"] == "text" else previous["visual_sample_intervals"]
        missing = gaps(known, pending["start_seconds"], pending["end_seconds"])
        if not missing and not resume:
            pending.update(status="completed", error="")
            store.write("video_progress", doc, old["version"], engine.identity(), doc["id"])
            continue
        blocked = store.read("preferences")["data"]
        if doc["owner_uid"] in blocked["blocked_uids"] or any(
                t.casefold() in doc["title"].casefold() for t in blocked["blocked_topics"] if t):
            pending.update(status="cancelled", error="blocked_target")
            store.write("video_progress", doc, old["version"], engine.identity(), doc["id"])
            continue
        start, end = ([cached["coverage"]["start_seconds"], cached["coverage"]["end_seconds"]]
                      if resume else missing[0])
        end = min(end, start + settings.get("media_max_seconds", 600))
        checks = [(k, None, store.read(k)["version"]) for k in CONFIG]
        goal = None
        if pending.get("goal"):
            goal = store.read("learning_goals", pending["goal"]["id"])
            if goal["version"] != pending["goal"]["version"] or goal["data"]["status"] != "active":
                pending.update(status="cancelled", error="goal_changed")
                store.write("video_progress", doc, old["version"], engine.identity(), doc["id"])
                continue
            checks.append(("learning_goals", goal["data"]["id"], goal["version"]))
            checks.append(("conversation_turns", goal["data"]["request"]["id"], goal["data"]["request"]["version"]))
        pending.update(status="reading", error="")
        with store.lock:
            if not resume:
                doc["windows_used"] += 1
            store.write("video_progress", doc, old["version"], engine.identity(), doc["id"])
            if not resume:
                store.set_state(daily, store.state(daily, 0) + 1)
        try:
            video = cached if resume else await engine.video_context(
                doc["bvid"], doc["page_index"], detail=pending["mode"] == "visual", start_seconds=start,
                end_seconds=end, continuation=True, gap_request=pending["id"],
            )
            scheduler.alive()
            verify_versions(store, checks)
            current = read(engine, doc["bvid"], doc["page_index"])
            active = next((r for r in current["data"]["requests"] if r == pending), None)
            if not active or current["data"]["locked"]:
                raise AssistantError("context_changed", "补读过程中本地请求或视频内容已变化")
            now = coverage(store, current)
            known_now = now["text_intervals"] if pending["mode"] == "text" else now["visual_sample_intervals"]
            if not resume and gaps(known_now, start, end) == gaps(known, start, end):
                raise AssistantError("video_gap_unavailable", "本轮没有取得缺口内的新材料，不自动重试")
            material = {"events": [], "video": video, "video_continuation": pending["id"]}
            if goal:
                material.update(learning_goal=goal["data"]["id"], learning_requirements={
                    k: goal["data"][k] for k in ("objective", "criteria", "gaps")})
            growth = store.read("growth")["version"]
            decision = await scheduler.decide(material)
            scheduler.alive()
            verify_versions(store, checks)
            verify_versions(store, [("video_progress", doc["id"], current["version"])])
            # Continuation is a reading job, not an extra opportunity to send an interaction.
            decision["action"] = None
            validate_decision(decision, material)
            saved = save_learning(engine, material, decision, growth)
            if saved["knowledge"]:
                store.audit("video", {"event": "gap_note", "source": ref(current), "knowledge": saved["knowledge"]})
                if goal:
                    from .research import attach

                    attached = attach(engine, goal["data"]["id"], saved["knowledge"], goal["version"])
                    for item in current["data"]["requests"]:
                        if item.get("goal") == pending["goal"]:
                            item["goal"] = ref(attached)
            active["status"] = "pending" if gaps(known_now, pending["start_seconds"], pending["end_seconds"]) else "completed"
            store.write("video_progress", current["data"], current["version"], engine.identity(), doc["id"])
            from_decision(engine, material, decision)
        except AssistantError as exc:
            current = read(engine, doc["bvid"], doc["page_index"])
            active = next((r for r in current["data"]["requests"] if r == pending), None)
            if active and not current["data"]["locked"]:
                active.update(status="blocked", error=exc.code)
                store.write("video_progress", current["data"], current["version"], engine.identity(), doc["id"])
        return True
    return False
