"""Platform-neutral editable conversation archive and compression context."""
import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime


def identifier(prefix, *parts):
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return prefix + "-" + hashlib.sha256(raw).hexdigest()[:32]


def thread_id(account, channel, key):
    return identifier("thread", account, channel, key)


def append(store, account, channel, key, message_id, content, speaker, sender_uid,
           status, recorded_at, occurred_at=None, reply_to="", sequence=0, attachments=(), *, on_write=None):
    """Stable per-message IDs make replay safe; local edits/deletions stay authoritative."""
    tid = thread_id(account, channel, key)
    receipts = f"conversation-recorded:{tid}:{message_id}"
    results = []
    transaction = store.db() if getattr(store, "backend", None) == "postgres" else nullcontext()
    with store.lock, transaction:
        # Receipts are recovery/dedupe state, not a second authoritative copy of the text.
        known = store.state(receipts, [])
        for part, offset in enumerate(range(0, max(1, len(content)), 12000)):
            item_id = identifier("turn", tid, message_id, part)
            if item_id in known:
                continue
            doc = {
                "schema_version": 1, "id": item_id, "thread_id": tid, "account_uid": account,
                "channel": channel, "thread_key": key, "speaker": speaker, "sender_uid": sender_uid,
                "content": content[offset:offset + 12000], "status": status, "message_id": message_id,
                "reply_to": reply_to, "part": part, "sequence": sequence, "occurred_at": occurred_at,
                "source": {"type": "conversation" if channel == "codex" else "interaction",
                           "recorded_at": recorded_at, "reference": message_id},
                "locked": False,
            }
            if attachments:
                doc["attachments"] = list(attachments)
            if not store.exists("conversation_turns", item_id):
                store.write("conversation_turns", doc, None, "conversation-recorder", item_id)
                if on_write:
                    on_write(item_id)
                results.append(item_id)
            known.append(item_id)
            store.set_state(receipts, known)
    return results


def records(store, account, tid):
    selected = [
        item for item in store.records("conversation_turns")[0]
        if item["data"]["account_uid"] == account and item["data"]["thread_id"] == tid
        and thread_id(account, item["data"]["channel"], item["data"]["thread_key"]) == tid
    ]

    def order(item):
        doc = item["data"]
        if doc["channel"] == "codex":
            return doc["sequence"], doc["part"], doc["id"]
        stamp = datetime.fromisoformat(doc["occurred_at"] or doc["source"]["recorded_at"])
        return stamp.timestamp(), doc["speaker"] == "assistant", doc["sequence"], doc["part"], doc["id"]

    return sorted(selected, key=order)


def context(store, account, tid, max_chars=None, *, query="", queries=()):
    from .conversation_recall import MAX_CHARS, recall, size
    from .memory_maintenance import best_summary

    settings = store.read("companion")["data"]
    limit = settings.get("conversation_context_turns", 24)
    budget = settings.get("conversation_context_chars", 12000) if max_chars is None else max_chars
    available = records(store, account, tid)
    summary = best_summary(store, account, tid, available)
    summary_payload = None
    if summary:
        summary_payload = {
            "id": summary["record"]["data"]["id"], "version": summary["record"]["version"],
            "content": summary["record"]["data"]["content"], "covered_turns": len(summary["covered"]),
        }
        if summary["partial"]:
            summary_payload["partial_turn"] = summary["partial"]
        summary_length = len(json.dumps(summary_payload, ensure_ascii=False))
        if summary_length <= budget - min(1000, budget // 3):
            budget -= summary_length
        else:
            summary = summary_payload = None
    remaining = available[len(summary["covered"]):] if summary else available
    if summary and summary["partial"] and remaining:
        # Prefix characters are represented by the summary; the unread suffix
        # remains a real turn and can never be counted as completely compressed.
        remaining = [deepcopy(remaining[0]), *remaining[1:]]
        partial = summary["partial"]
        remaining[0]["data"]["content"] = remaining[0]["data"]["content"][partial["end"]:]
        remaining[0]["content_span"] = {**partial, "start": partial["end"], "end": partial["total"]}
    def tail(allowance):
        selected, used = [], 0
        for item in reversed(remaining):
            length = size(item)
            if not selected and length > allowance:
                item = deepcopy(item)
                item["content_truncated"] = True
                chars = max(0, len(item["data"]["content"]) - (length - allowance) - 80)
                item["data"]["content"] = item["data"]["content"][:chars]
                length = size(item)
            if len(selected) >= limit or used + length > allowance:
                # Keep the recent tail contiguous, even when recall has a separate window.
                break
            selected.append(item)
            used += length
        return selected, used

    selected, used = tail(budget)
    recalled = []
    if settings.get("conversation_recall_enabled", True) and (query.strip() or queries):
        # Protect at least half the post-summary budget for the latest messages.
        # A miss consumes no space; fully supplied recent turns are not repeated.
        excluded = {item["data"]["id"] for item in selected
                    if not item.get("content_truncated") and not item.get("content_span")}
        recalled = recall(available, query, queries, excluded=excluded,
                          max_chars=min(MAX_CHARS, budget // 2))
        if recalled:
            selected, used = tail(budget - size(recalled))
    return {"thread_id": tid, "turns": list(reversed(selected)), "total_turns": len(available),
            "summary": summary_payload, "recalled": recalled,
            "truncated": len(remaining) != len(selected) or any(r.get("content_truncated") for r in selected),
            "context_chars": used + (size(summary_payload) if summary else 0) + (size(recalled) if recalled else 0)}
