"""Explicit comment mentions, including protection against accidental @ words."""

import re

from .errors import AssistantError


def validate_mentions(text, mentions):
    if not isinstance(mentions, dict) or len(mentions) > 5:
        raise AssistantError("invalid_mentions", "mentions 必须为最多 5 个用户名到 UID 的映射")
    remaining = text
    for name, uid in mentions.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 60
            or "@" in name
            or any(c.isspace() for c in name)
            or not isinstance(uid, str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", uid)
        ):
            raise AssistantError("invalid_mentions", "提及需要完整用户名和字符串形式的 UID")
        remaining, count = re.subn(r"@" + re.escape(name) + r"(?=\s|$)", "", remaining)
        if not count:
            raise AssistantError("invalid_mentions", "正文需包含对应 @用户名，后接空格或换行")
    if "@" in remaining:
        raise AssistantError("unmapped_mention", "正文含未指定目标的 @，可能误提及其他用户")
    return mentions
