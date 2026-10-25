"""Authenticated, encrypted image input; no filesystem/URL input from a message."""
import hashlib
import io
import secrets
import warnings

from .access import AccessService, RecordPolicy
from .errors import AssistantError
from .governance import GovernanceRepository
from .record_maintenance import remove
from .schema import obj

MAX_BYTES = 8 * 1024 * 1024
SCHEMA = obj({"id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
    "width": {"type": "integer", "minimum": 1, "maximum": 2048},
    "height": {"type": "integer", "minimum": 1, "maximum": 2048},
    "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    "bytes": {"type": "integer", "minimum": 1, "maximum": MAX_BYTES},
    "format": {"const": "PNG"}, "locked": {"const": True}})


class InputImages:
    def __init__(self, store, authority):
        self.store, self.authority = store, authority
        self.repository = GovernanceRepository(store)
        self.access = AccessService(self.repository, authority)

    def upload(self, request, blob):
        self.authority.require(request)
        if request.audience != "private" or request.purpose != "reply" or not request.session_id:
            raise AssistantError("access_denied", "图片上传需要已登录的私密会话")
        if not blob or len(blob) > MAX_BYTES:
            raise AssistantError("image_limit", "每张图片不能超过 8 MB")
        from PIL import Image, ImageOps, UnidentifiedImageError

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(blob)) as image:
                    if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 16_000_000:
                        raise ValueError()
                    image.load()
                    oriented = ImageOps.exif_transpose(image)
                    # Render transparent pixels on the page's light background;
                    # hidden RGB under alpha must not become model-visible data.
                    visible = Image.new("RGBA", oriented.size, "white")
                    visible.alpha_composite(oriented.convert("RGBA"))
                    clean = Image.new("RGB", oriented.size)
                    clean.paste(visible.convert("RGB"))
                    clean.thumbnail((2048, 2048))
                    output = io.BytesIO()
                    clean.save(output, format="PNG")
                    raw = output.getvalue()
                    width, height = clean.size
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise AssistantError("invalid_image", "请上传不超过 1600 万像素的 PNG、JPEG 或 WebP 图片") from None
        if len(raw) > MAX_BYTES:
            raise AssistantError("image_limit", "图片规范化后超过 8 MB，请缩小后重试")
        identifier, version = secrets.token_hex(16), hashlib.sha256(raw).hexdigest()
        now = self.repository.clock()
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.authority.require(request)
            # Bound abandoned uploads. Purge only expired, unbound images; sent inputs remain governed records.
            expired = [row[0] for row in db.execute("SELECT id FROM input_image_blobs WHERE event_id IS NULL AND created_at<?", (now - 86400,))]
            for old in expired:
                old_record = self.store.read("input_images", old)
                remove(self.store, "input_images", old, old_record["version"], "expired-upload")
            count = db.execute("SELECT count(*) FROM input_image_blobs WHERE actor=? AND event_id IS NULL", (request.actor,)).fetchone()[0]
            if count >= 12:
                raise AssistantError("image_limit", "待发送图片已达 12 张，请先发送或移除")
            data = {"id": identifier, "width": width, "height": height, "sha256": version,
                    "bytes": len(raw), "format": "PNG", "locked": True}
            self.store.write("input_images", data, None, request.actor, identifier)
            old = self.repository.policy("input_images/" + identifier)
            self.repository.set_policy(RecordPolicy(old.resource, secrets.token_hex(16), "conversation",
                frozenset((request.actor,)), request.conversation, "private"), old.version, request.actor)
            db.execute("INSERT INTO input_image_blobs VALUES(?,?,?,?,?,?,NULL)", (identifier, version,
                       self.store.cipher.encrypt(raw, "input_images", identifier, version),
                       request.actor, request.conversation, now))
        return data

    def read(self, request, identifier):
        self.authority.require(request)
        resource = "input_images/" + identifier
        lease = self.access.lease(request, [resource])
        with self.store.db() as db:
            row = db.execute("SELECT * FROM input_image_blobs WHERE id=?", (identifier,)).fetchone()
        if not row or row["actor"] != request.actor or row["conversation"] != request.conversation:
            raise AssistantError("access_denied", "图片不属于当前会话")
        raw = self.store.cipher.decrypt(bytes(row["body"]), "input_images", identifier, row["version"])
        metadata = self.store.read("input_images", identifier)["data"]
        if hashlib.sha256(raw).hexdigest() != metadata["sha256"] or metadata["sha256"] != row["version"]:
            raise AssistantError("storage_integrity", "图片完整性校验失败")
        self.access.revalidate(lease)
        return raw

    def bind(self, request, identifiers, event):
        if (not isinstance(identifiers, list) or len(identifiers) > 3
                or any(not isinstance(key, str) for key in identifiers) or len(set(identifiers)) != len(identifiers)):
            raise AssistantError("image_limit", "每条消息最多 3 张不同的图片")
        attachments = []
        for identifier in identifiers:
            if not isinstance(identifier, str) or len(identifier) != 32:
                raise AssistantError("invalid_image", "图片标识无效")
            self.read(request, identifier)
            with self.store.db() as db:
                row = db.execute("SELECT event_id FROM input_image_blobs WHERE id=? FOR UPDATE", (identifier,)).fetchone()
                if row[0] not in (None, event.id):
                    raise AssistantError("image_used", "图片已用于另一条消息，请重新上传")
                db.execute("UPDATE input_image_blobs SET event_id=? WHERE id=?", (event.id, identifier))
            attachments.append({"kind": "image", "url": "input-image:" + identifier, "title": "用户上传图片", "bvid": ""})
        return tuple(attachments)

    def discard(self, request, identifier):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.read(request, identifier)
            row = db.execute("SELECT event_id FROM input_image_blobs WHERE id=?", (identifier,)).fetchone()
            if row[0]:
                raise AssistantError("image_used", "已发送图片请在管理端查看删除影响")
            item = self.store.read("input_images", identifier)
            remove(self.store, "input_images", identifier, item["version"], request.actor)
        return {"removed": True}


def identifiers(event):
    result = []
    for item in event.attachments:
        value = item.get("url", "")
        if item.get("kind") != "image" or not value.startswith("input-image:") or len(value) != 44:
            raise AssistantError("invalid_image", "消息图片引用无效")
        result.append(value[12:])
    return result


def verify_blobs(store):
    """Validate an administrative restore before committing any restored data."""
    with store.db() as db:
        expected = {row[0] for row in db.execute("SELECT id FROM content_records WHERE kind='input_images'")}
        seen = set()
        for row in db.execute("SELECT * FROM input_image_blobs"):
            identifier = row['id']
            if identifier not in expected:
                raise AssistantError('backup_invalid', '恢复图片缺少对应资料')
            metadata = store.read('input_images', identifier)['data']
            raw = store.cipher.decrypt(bytes(row['body']), 'input_images', identifier, row['version'])
            if (hashlib.sha256(raw).hexdigest() != row['version'] or row['version'] != metadata['sha256']
                    or len(raw) != metadata['bytes'] or len(raw) > MAX_BYTES):
                raise AssistantError('backup_invalid', '恢复图片未通过完整性校验')
            seen.add(identifier)
        if expected != seen:
            raise AssistantError('backup_invalid', '恢复图片文件不完整')
