"""Registered images only; authenticated restore jobs keep failed restores paused."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import secrets
from pathlib import Path

from PIL import Image

from .errors import AssistantError
from .storage import atomic_write


def registry(store):
    result = {}
    visual = store.read("persona")["data"].get("visual_identity") or {}
    for ref in visual.get("references", []):
        result["assets/persona/" + ref["filename"]] = ref["sha256"]
    if "publication_images" in store.profile.schemas:
        for item in store.records("publication_images")[0]:
            doc = item["data"]
            result["assets/publication/" + doc["filename"]] = doc["sha256"]
    if "task_assets" in store.profile.schemas:
        for item in store.records("task_assets")[0]:
            doc = item["data"]
            result["assets/tasks/" + doc["filename"]] = doc["sha256"]
    return result


def _path(store, relative):
    if (not re.fullmatch(r"assets/(?:persona|publication|tasks)/[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp)", relative, re.I)
            or ".." in relative):
        raise AssistantError("asset_path", "图片备份包含无效路径")
    path = store.root / Path(relative)
    if (not path.resolve().is_relative_to(store.root.resolve())
            or any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent))):
        raise AssistantError("asset_path", "图片路径离开了资料目录")
    return path


def _validate(raw, expected):
    if not 0 < len(raw) <= 10_000_000 or hashlib.sha256(raw).hexdigest() != expected:
        raise AssistantError("asset_integrity", "登记图片大小或摘要不匹配")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP") or image.width * image.height > 20_000_000:
                raise ValueError()
            image.verify()
    except (ValueError, OSError, Image.DecompressionBombError):
        raise AssistantError("asset_integrity", "登记图片格式无效") from None


def capture(store):
    result, total = [], 0
    for relative, expected in registry(store).items():
        path = _path(store, relative)
        if not path.is_file() or path.stat().st_size > 10_000_000:
            raise AssistantError("asset_missing", "登记图片缺失，不能声称备份完整")
        raw = path.read_bytes()
        total += len(raw)
        if total > 128_000_000:
            raise AssistantError("asset_limit", "登记图片超过本次备份上限")
        _validate(raw, expected)
        result.append({"path": relative, "sha256": expected, "body": base64.b64encode(raw).decode("ascii")})
    return result


def enqueue(store, assets, db):
    expected = registry(store)
    if {item["path"] for item in assets} != set(expected) or len(assets) != len(expected):
        raise AssistantError("asset_integrity", "备份图片与登记资料不一致")
    for item in assets:
        _path(store, item["path"])
        raw = base64.b64decode(item["body"], validate=True)
        _validate(raw, expected[item["path"]])
        identifier = secrets.token_hex(16)
        encrypted = store.cipher.encrypt(raw, "asset-restore", identifier, item["sha256"])
        db.execute("INSERT INTO asset_restore_jobs VALUES(?,?,?,?)",
                   (identifier, item["path"], item["sha256"], encrypted))


def finish(store):
    completed = 0
    with store.lock:
        with store.db() as db:
            jobs = list(db.execute("SELECT * FROM asset_restore_jobs ORDER BY id"))
        for job in jobs:
            raw = store.cipher.decrypt(bytes(job["body"]), "asset-restore", job["id"], job["digest"])
            _validate(raw, job["digest"])
            path = _path(store, job["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, raw)
            if hashlib.sha256(path.read_bytes()).hexdigest() != job["digest"]:
                raise AssistantError("asset_integrity", "恢复图片落盘校验失败")
            with store.db() as db:
                db.execute("DELETE FROM asset_restore_jobs WHERE id=?", (job["id"],))
            completed += 1
    return {"assets_restored": completed, "role_started": False}
