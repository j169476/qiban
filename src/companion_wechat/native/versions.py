"""Single version registry shared by the reader and generated native bridge.

An ABI change requires implementation review, not just another address table.
Import performs no process access. Invalid/incomplete manifests fail closed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CANONICAL = {
    "app_global": 0xB912690, "app_vtable": 0x8D804E8, "app_getter": 0x469A0,
    "identity_vtable": 0x8DFE2F8, "identity_getter": 0x36F930,
    "app": 0x45D10, "scheduler": 0x47310, "schedule": 0x2FB010,
    "text_builder": 0x54B02F0, "allocate": 0x74AB89C, "free": 0x74AB8E0,
}
ACCOUNT = ("app_global", "app_vtable", "app_getter", "identity_vtable", "identity_getter")


def load_profiles(path):
    value = json.loads(Path(path).read_text(encoding="utf8"))
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("profiles"), list):
        raise ValueError("invalid_wechat_manifest")
    profiles, seen_ids, seen_versions, seen_hashes, seen_memory = [], set(), set(), set(), set()
    for raw in value["profiles"]:
        try:
            p = dict(raw)
            if (type(p["id"]) is not int or not 0 < p["id"] < 65536
                    or type(p["size"]) is not int or p["size"] <= 0
                    or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p["version"])
                    or not re.fullmatch(r"[0-9a-f]{64}", p["sha256"])
                    or p["abi"] != "msvc-text-v1" or set(p["rvas"]) != set(CANONICAL)):
                raise ValueError
            for field in ("timestamp", "image_size"):
                p[field] = int(p[field], 16)
                if not 0 < p[field] <= 0xFFFFFFFF:
                    raise ValueError
            p["rvas"] = {name: int(rva, 16) for name, rva in p["rvas"].items()}
            if any(not 0 < rva < p["image_size"] for rva in p["rvas"].values()):
                raise ValueError
            memory = (p["timestamp"], p["image_size"])
            if (p["id"] in seen_ids or p["version"] in seen_versions
                    or p["sha256"] in seen_hashes or memory in seen_memory):
                raise ValueError
            seen_ids.add(p["id"])
            seen_versions.add(p["version"])
            seen_hashes.add(p["sha256"])
            seen_memory.add(memory)
            p["account_rvas"] = tuple(p["rvas"][name] for name in ACCOUNT)
            profiles.append(p)
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid_wechat_profile") from None
    if not profiles:
        raise ValueError("empty_wechat_manifest")
    return tuple(profiles)


PROFILES = load_profiles(Path(__file__).with_name("versions.json"))
