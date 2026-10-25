"""Copy only this worker's generated image out of a disposable sandbox."""
import hashlib
import re
import uuid
from pathlib import Path

from PIL import Image

from companion_core.errors import AssistantError


def export_image(job, evidence, decision, runtime):
    thread = evidence.thread_id
    if not re.fullmatch(r"[a-f0-9-]{36}", thread):
        raise AssistantError("native_image_evidence", "缺少本轮图片会话证据")
    root = job.home / "generated_images" / thread
    source = Path(decision["image_path"])
    if (not source.is_absolute() or source.is_symlink() or source.resolve().parent != root
            or not source.is_file() or source.stat().st_mtime < evidence.trace["started"] - 2
            or source.stat().st_size > 20_000_000):
        raise AssistantError("native_image_evidence", "图片不属于本轮隔离生成任务")
    if evidence.images and str(source) not in evidence.images:
        raise AssistantError("native_image_evidence", "图片与原生工具事件不一致")
    destination = runtime / "generated-images" / (uuid.uuid4().hex + ".png")
    destination.parent.mkdir(exist_ok=True)
    try:
        with Image.open(source) as picture:
            if picture.width * picture.height > 25_000_000:
                raise ValueError()
            picture.convert("RGBA").save(destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    except (OSError, ValueError, Image.DecompressionBombError):
        destination.unlink(missing_ok=True)
        raise AssistantError("native_image_evidence", "生成图片无法安全导出") from None
    decision["image_path"] = str(destination.resolve())
    evidence.trace["isolated_image"] = {"path": str(destination.resolve()), "sha256": digest}
