"""Lexical recall of already-authorized raw turns, with versioned source excerpts."""
import json
from copy import deepcopy

from .passages import LONG_RECORD_CHARS, excerpt, metadata, split, terms
from .retrieval import scores

MAX_CHARS = 6000
MAX_ANCHORS = 2
GUIDANCE = (
    "conversation.recalled 是按当前问题选出的历史原文片段，不是连续对话或已确认事实。"
    "按 speaker、sender_uid、时间和 status 区分用户陈述、助手回答及未成功发送的内容。"
    "结合相邻消息与后续更正理解原意；excerpt 标明实际提供的原文范围。"
    "未召回或被截断的内容仍是未知，不能声称已读完历史，也不能把相似人物或条件合并。"
)


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def recall(available, query, queries=(), *, excluded=(), max_chars=MAX_CHARS):
    """Caller scopes/authorizes the corpus BEFORE this function sees any text.

    Keep at most two disjoint three-turn windows. Neighbors are data, not extra
    matches. A window is supplied atomically so a tight budget cannot silently
    keep an assertion while dropping its adjacent correction. No disk index or
    extra model call is used. Coordinates always refer to the original turn.
    """
    query_terms = terms(query, queries)
    if not query_terms or max_chars < 1:
        return []
    excluded = set(excluded)
    candidates = [{"kind": "conversation_turns", **item} for item in available
                  if item["data"]["id"] not in excluded and item["data"]["content"]]
    spans = {("conversation_turns", item["data"]["id"]): split(item["data"]["content"])
             for item in candidates if len(item["data"]["content"]) > LONG_RECORD_CHARS}
    ranked = scores(candidates, query, queries, passages=spans)
    positions = {item["data"]["id"]: index for index, item in enumerate(available)}
    matches = sorted((key[1] for key, score in ranked.items() if score > 0),
                     key=lambda key: (ranked["conversation_turns", key], positions[key]), reverse=True)
    windows, claimed = [], set()
    for key in matches:
        position = positions[key]
        if position in claimed:
            continue
        window = [i for i in range(max(0, position - 1), min(len(available), position + 2))
                  if i not in claimed and available[i]["data"]["id"] not in excluded
                  and available[i]["data"]["content"]]
        windows.append((key, window))
        claimed.update(window)
        if len(windows) == MAX_ANCHORS:
            break
    result = []
    for window_index, (anchor, window) in enumerate(windows):
        # Prefer a complete small window over fragmenting it to fit more hits.
        # If this window is too large, each turn gets its own bounded excerpt.
        views = []
        for index in window:
            item = available[index]
            data = item["data"]
            view = {"kind": "conversation_turns", "version": item["version"],
                    "data": {field: deepcopy(data[field]) for field in (
                        "id", "content", "speaker", "sender_uid", "status", "occurred_at",
                        "message_id", "reply_to", "part", "sequence")},
                    "recorded_at": data["source"]["recorded_at"],
                    "recall_anchor": anchor, "recall_match": data["id"] == anchor}
            view["excerpt"] = metadata(view, [[0, len(data["content"])]])
            views.append(view)
        remaining = max_chars - size(result) - 2 * len(views)
        # Reserve space for the other matched window, including a distant
        # correction. If even the metadata cannot fit, prefer the stronger hit.
        share = remaining // (len(windows) - window_index)
        for allocation in dict.fromkeys((share, remaining)):
            if size(views) <= allocation:
                packed = views
            else:
                allowance = max(0, allocation // len(views) - 2)
                packed = [excerpt(view, spans.get(("conversation_turns", view["data"]["id"]), []),
                                  query_terms, allowance) for view in views]
            if all(view is not None for view in packed):
                break
        else:
            continue
        views = packed
        if size([*result, *views]) <= max_chars:
            result.extend(views)
    return sorted(result, key=lambda item: positions[item["data"]["id"]])
