"""Bounded metadata parsing; visible @names and quoted XML text never wake a bot."""
import xml.etree.ElementTree as ET

from .db_text import short_text


def xml(value, tag):
    text = short_text(value)
    if not text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return None
    try:
        root = ET.fromstring(text)
        return root if root.tag == tag and sum(1 for _ in root.iter()) <= 128 else None
    except (ET.ParseError, ValueError):
        return None


def message(kind, content, source, self_username, *, own_reference):
    """own_reference must verify a server ID in this exact source conversation."""
    if kind & 0xFFFFFFFF == 1:
        text = short_text(content)
        meta = xml(source, "msgsource")
        members = (meta.findtext("atuserlist") or "").split(",") if meta is not None else []
        return text, "mention" if self_username in [m.strip() for m in members] else "none"
    if kind & 0xFFFFFFFF != 49:
        return None, "none"
    root = xml(content, "msg")
    app = root.find("appmsg") if root is not None else None
    if app is None or app.findtext("type") != "57":
        return None, "none"
    title = app.findtext("title")
    reference = app.find("refermsg")
    if not title or reference is None:
        return None, "none"
    identifier = reference.findtext("svrid") or ""
    own = (reference.findtext("fromusr") == self_username and identifier.isascii()
           and identifier.isdecimal() and 0 < len(identifier) <= 19 and 0 < int(identifier) <= 2**63 - 1
           and own_reference(int(identifier)))
    return title, "reply" if own else "none"
