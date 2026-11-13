"""Verified native records to core events; cache-local locators never identify events.

Only the owned native receiver supplies the verified catalog. This function is
not a model tool or an HTTP endpoint. Media extraction is a separate stage.
"""
import re
from datetime import datetime, timezone

from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.reply_authorization import require

from .config import qq_id


def _uid(value):
    require(isinstance(value, str) and re.fullmatch(r"u_[A-Za-z0-9_-]{1,128}", value), "qq_identity_invalid")
    return value


def _number(value, *, positive=False):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value), "qq_event_invalid")
    require(not positive or int(value) > 0, "qq_event_invalid")
    return str(int(value))


def normalize_native(raw, options, *, identity, sent_reference):
    require(isinstance(raw, dict) and isinstance(identity, dict), "qq_event_invalid")
    require(qq_id(identity.get("uin")) == options["account"], "qq_account_mismatch")
    self_uid = _uid(identity.get("uid"))
    require(isinstance(identity.get("friends"), dict) and isinstance(identity.get("groups"), list),
            "qq_catalog_invalid")
    if raw.get("chatType") not in (1, 2):
        return None
    if (raw["chatType"] == 2 and raw.get("msgType") == 5 and str(raw.get("senderUin")) == "0"
            and raw.get("senderUid") == "" and isinstance(raw.get("elements"), list) and raw["elements"]
            and all(isinstance(p, dict) and p.get("elementType") == 8 for p in raw["elements"])):
        return None
    sender, sender_uid = qq_id(raw.get("senderUin")), _uid(raw.get("senderUid"))
    if sender == options["account"]:
        require(sender_uid == self_uid, "qq_account_mismatch")
        return None
    require(sender_uid != self_uid, "qq_identity_invalid")
    kind = "private" if raw["chatType"] == 1 else "group"
    if kind == "private":
        target = sender
        if target not in options["private_targets"]:
            return None
        require(identity["friends"].get(target) == sender_uid == raw.get("peerUid"), "qq_target_unverified")
    else:
        # The observed anonymous system-tip form is handled by the receiver
        # before normalization; no blank author can create a conversation actor.
        target = qq_id(raw.get("peerUid"))
        if target not in options["groups"]:
            return None
        require(target in identity["groups"], "qq_target_unverified")
    stamp = int(_number(raw.get("msgTime"), positive=True))
    require(stamp < 4102444800, "qq_event_invalid")
    parts = raw.get("elements")
    require(isinstance(parts, list) and len(parts) <= 200, "qq_array_messages_required")
    text, media, trigger = [], [], "direct" if kind == "private" else "none"
    media_types = {2: ("image", "picElement"), 3: ("file", "fileElement"), 4: ("audio", "pttElement")}
    for position, part in enumerate(parts):
        require(isinstance(part, dict), "qq_event_invalid")
        if part.get("elementType") == 1:
            element = part.get("textElement")
            require(isinstance(element, dict) and isinstance(element.get("content"), str), "qq_event_invalid")
            text.append(element["content"])
            if (kind == "group" and trigger != "reply" and element.get("atType") == 2
                    and str(element.get("atUid")) == options["account"] and element.get("atNtUid") == self_uid):
                trigger = "mention"
        elif kind == "group" and part.get("elementType") == 7:
            reply = part.get("replyElement")
            require(isinstance(reply, dict), "qq_event_invalid")
            if (str(reply.get("senderUid")) == options["account"] and reply.get("senderUidStr") == self_uid
                    and sent_reference(kind, target, "native-seq:" + _number(reply.get("replayMsgSeq"), positive=True))):
                trigger = "reply"
        elif part.get("elementType") in media_types:
            media_kind, field = media_types[part["elementType"]]
            require(isinstance(part.get(field), dict), "qq_media_invalid")
            # A native elementId, download path, size, or thumbnail can change
            # when the cache is rebuilt or a download completes. They must not
            # change the canonical event or become model-supplied file paths.
            media.append({"kind": media_kind, "position": position})
    content = "".join(text).strip()
    if not content and media:
        names = {"image": "图片", "audio": "语音", "file": "文件"}
        description = "、".join(names[value] for value in dict.fromkeys(item["kind"] for item in media))
        content = "（收到" + description + "附件；内容尚未读取）"
    if not content and trigger in {"mention", "reply"}:
        content = "（对方唤起了你，未附带可读取的文本）"
    if not content:
        return None
    require(len(content.encode("utf-8")) <= 16000, "qq_message_oversize")
    omitted = max(0, len(media) - 12)
    if omitted:
        content += f"\n（本条消息另有 {omitted} 个附件超过接收数量限制，未纳入附件列表。）"
    source = ([_number(raw.get("clientSeq")), _number(raw.get("msgRandom"))] if kind == "private"
              else [_number(raw.get("msgSeq"), positive=True)])
    native = stable_key("qq-native-message", options["account"], self_uid, kind, target,
                        sender, sender_uid, stamp, *source)
    attachments = tuple({"id": stable_key("qq-native-attachment", native, item["position"], item["kind"]),
                         **item} for item in media[:12])
    return Event("qq", options["account"], native, ActorRef("qq", sender, options["account"]),
                 kind + ":" + target, datetime.fromtimestamp(stamp, timezone.utc).isoformat(), content, kind,
                 attachments=attachments,
                 metadata={"qq": {"target_id": target, "trigger": trigger, "identity_quality": "verified",
                                   **({"attachments_omitted": omitted} if omitted else {})}})
