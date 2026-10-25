"""Evidence for Codex selection, with correction and per-video deduplication."""

from companion_core.interests import build_profile

from .storage import recorded_time


def interest_profile(store):
    guide = store.read("companion")["data"]["guide_uid"]
    signals = sorted(
        (
            item for item in store.records("interest_signals")[0]
            if guide and item["data"]["sender_uid"] == guide
        ),
        key=lambda item: (recorded_time(item["data"]), item["data"]["id"]),
        reverse=True,
    )
    normalized = [{**r, "data": {**r["data"], "content_id": r["data"]["bvid"]}} for r in signals]
    result = build_profile(normalized, guide)
    for topic in result["topics"]:
        topic["supporting_videos"] = topic.pop("supporting_sources")
        topic["distinct_videos"] = topic.pop("distinct_sources")
        topic["status"] = topic["status"].replace("独立内容", "独立视频")
    result["note"] = result["note"].replace("同一内容", "同一视频")
    return result
