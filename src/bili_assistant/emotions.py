"""Bilibili material evidence over shared core emotion rules."""
from companion_core.emotions import apply as apply
from companion_core.emotions import available as available
from companion_core.emotions import current as current
from companion_core.emotions import evidence_key as evidence_key
from companion_core.emotions import recover as recover


def material_evidence(material):
    ids = [e["id"] for e in material.get("events", [])]
    video = material.get("video")
    if video and video["coverage"]["kind"] != "metadata_only":
        ids.append("video:" + video["bvid"])
    return ids
