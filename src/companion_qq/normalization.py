"""Only authenticated structured OneBot events become scoped core events."""
from datetime import datetime, timezone

from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.reply_authorization import require

from .config import qq_id
from .transport import message_id


def normalize(raw, options, *, sent_reference):
    require(isinstance(raw, dict), "qq_event_invalid")
    if raw.get("post_type") != "message":
        return None
    require(qq_id(raw.get("self_id")) == options["account"], "qq_account_mismatch")
    kind = raw.get("message_type")
    if kind not in {"private", "group"}:
        return None
    sender = qq_id(raw.get("user_id"))
    if sender == options["account"]:
        return None
    if kind == "private" and raw.get("sub_type") != "friend":
        return None  # Temporary sessions don't inherit group permission.
    target = sender if kind == "private" else qq_id(raw.get("group_id"))
    if target not in options["private_targets" if kind == "private" else "groups"]:
        return None
    stamp = raw.get("time")
    require(type(stamp) is int and 1 <= stamp < 4102444800, "qq_event_invalid")
    segments = raw.get("message")
    require(isinstance(segments, list) and len(segments) <= 200, "qq_array_messages_required")
    text, trigger = [], "direct" if kind == "private" else "none"
    for part in segments:
        require(isinstance(part, dict) and isinstance(part.get("data"), dict), "qq_event_invalid")
        data = part["data"]
        if part.get("type") == "text":
            require(isinstance(data.get("text"), str), "qq_event_invalid")
            text.append(data["text"])
        elif kind == "group" and part.get("type") == "at" and str(data.get("qq")) == options["account"]:
            trigger = "mention"
        elif kind == "group" and part.get("type") == "reply":
            if sent_reference(kind, target, message_id(data.get("id"))):
                trigger = "reply"
    content = "".join(text).strip()
    if not content and trigger in {"mention", "reply"}:
        content = "（对方唤起了你，未附带可读取的文本）"
    if not content:
        return None
    require(len(content.encode("utf-8")) <= 16000, "qq_message_oversize")
    native = stable_key("qq-message", kind, target, message_id(raw.get("message_id")))
    return Event("qq", options["account"], native, ActorRef("qq", sender, options["account"]),
                 kind + ":" + target, datetime.fromtimestamp(stamp, timezone.utc).isoformat(), content, kind,
                 metadata={"qq": {"target_id": target, "trigger": trigger, "identity_quality": "verified"}})
