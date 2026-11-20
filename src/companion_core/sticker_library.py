"""Administrator-imported shared stickers; selection has no rate or repeat filter."""

import base64
import hashlib
import io
import secrets
import time
import warnings
from pathlib import Path

from .errors import AssistantError
from .governance import GovernanceRepository

MAX_BYTES = 10 * 1024 * 1024


def check(ok, code="sticker_invalid", message="表情资料无效，请重新读取。"):
    if not ok:
        raise AssistantError(code, message)


def describe(name, tags):
    check(isinstance(name, str) and 0 < len(name.strip()) <= 100, message="表情名称须为1至100个字符。")
    check(
        isinstance(tags, list)
        and len(tags) <= 20
        and all(isinstance(tag, str) and 0 < len(tag.strip()) <= 40 for tag in tags),
        message="最多20个标签，每个标签1至40个字符。",
    )
    return {"name": name.strip(), "tags": list(dict.fromkeys(tag.strip() for tag in tags))}


def inspect(content):
    from PIL import Image, ImageOps

    check(isinstance(content, bytes) and 0 < len(content) <= MAX_BYTES, message="每张表情最多10 MiB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                check(
                    image.format in {"PNG", "JPEG", "WEBP", "GIF"}, message="支持PNG、JPEG、WebP和GIF表情。"
                )
                count = getattr(image, "n_frames", 1)
                check(
                    1 <= count <= 1000
                    and image.width * image.height <= 16_000_000
                    and count * image.width * image.height <= 64_000_000,
                    message="表情像素或动画帧数过大，请缩小后导入。",
                )
                details = {
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "frames": count,
                }
                for frame in range(count):
                    image.seek(frame)
                    image.load()
                image.seek(0)
                thumbnail = ImageOps.exif_transpose(image).convert("RGBA")
                thumbnail.thumbnail((64, 64))
                output = io.BytesIO()
                thumbnail.save(output, format="PNG")
                details["preview"] = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(
                    "ascii"
                )
                return details
    except AssistantError:
        raise
    except (OSError, ValueError, EOFError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AssistantError("sticker_invalid", "图片不能完整解码，请选择有效表情文件。") from None


class StickerLibrary:
    def __init__(self, store):
        self.store = store

    def import_file(self, path, name, tags, *, actor):
        # Only the authenticated desktop manager calls this; no model path input.
        describe(name, tags)
        check(isinstance(path, str) and Path(path).is_absolute(), message="请通过文件选择器选择表情。")
        try:
            with Path(path).open("rb") as stream:
                content = stream.read(MAX_BYTES + 1)
        except OSError:
            raise AssistantError("sticker_file", "无法读取所选图片，请重新选择。") from None
        return self.import_bytes(content, name, tags, actor=actor)

    def import_bytes(self, content, name, tags, *, actor):
        metadata = {**describe(name, tags), **inspect(content)}
        digest = hashlib.sha256(content).hexdigest()
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM sticker_library WHERE sha256=? ORDER BY created_at,id LIMIT 1",
                             (digest,)).fetchone()
            if old:
                return {"item": self._summary(old), "duplicate": True}
            from .media_originals import MediaOriginals

            MediaOriginals(self.store)._make_room(db, len(content), lambda: None)
            identifier, revision = secrets.token_hex(16), secrets.token_hex(16)
            db.execute(
                "INSERT INTO sticker_library VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    digest,
                    len(content),
                    self.store.ledger_encode("sticker:" + identifier, metadata),
                    self.store.cipher.encrypt(content, "stickers", identifier, digest),
                    revision,
                    False,
                    time.time(),
                ),
            )
            GovernanceRepository(self.store).audit(
                actor, "sticker_imported", "sticker:" + identifier, None, revision, db=db
            )
            return {"item": self.get(identifier, db=db), "duplicate": False}

    def _summary(self, row):
        metadata = self.store.ledger_decode("sticker:" + row["id"], row["metadata"])
        return {
            **metadata,
            "id": row["id"],
            "revision": row["revision"],
            "enabled": row["enabled"],
            "bytes": row["size"],
            "sha256": row["sha256"],
        }

    def get(self, identifier, *, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.get(identifier, db=connection)
        row = db.execute("SELECT * FROM sticker_library WHERE id=?", (identifier,)).fetchone()
        check(row is not None, "sticker_missing", "表情不存在或已移除。")
        return self._summary(row)

    def listing(self, after="", limit=5):
        check(isinstance(after, str) and type(limit) is int and 1 <= limit <= 5)
        with self.store.db() as db:
            return {
                "items": [
                    self._summary(row)
                    for row in db.execute(
                        "SELECT * FROM sticker_library WHERE id>? ORDER BY id LIMIT ?", (after, limit)
                    )
                ]
            }

    def configure(self, identifier, name, tags, enabled, expected, *, actor):
        values = describe(name, tags)
        check(type(enabled) is bool)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sticker_library WHERE id=? FOR UPDATE", (identifier,)).fetchone()
            check(row is not None, "sticker_missing", "表情不存在或已移除。")
            check(row["revision"] == expected, "edit_conflict", "表情已改变，请重新读取。")
            metadata = self.store.ledger_decode("sticker:" + identifier, row["metadata"])
            if (values["name"], values["tags"]) != (metadata["name"], metadata["tags"]):
                from .library_versions import LibraryVersions

                LibraryVersions(self.store, "stickers").archive(db, row)
            revision = secrets.token_hex(16)
            db.execute(
                "UPDATE sticker_library SET metadata=?,enabled=?,revision=? WHERE id=?",
                (
                    self.store.ledger_encode("sticker:" + identifier, {**metadata, **values}),
                    enabled,
                    revision,
                    identifier,
                ),
            )
            GovernanceRepository(self.store).audit(
                actor, "sticker_changed", "sticker:" + identifier, expected, revision, db=db
            )
            return self.get(identifier, db=db)

    def candidates(self, text, *, limit=12):
        """Bound context size, not usage. A recently sent sticker remains eligible."""
        check(isinstance(text, str) and type(limit) is int and 1 <= limit <= 50)
        ranked = []
        with self.store.db() as db:
            for row in db.execute(
                "SELECT id,sha256,size,metadata,revision,enabled FROM sticker_library WHERE enabled ORDER BY id"
            ):
                item = self._summary(row)
                score = sum(tag.casefold() in text.casefold() for tag in [item["name"], *item["tags"]])
                ranked.append(
                    (-score, item["id"], {key: item[key] for key in ("id", "revision", "name", "tags")})
                )
        return [item for _, _, item in sorted(ranked)[:limit]]

    def read_for_send(self, identifier, expected, *, guard):
        guard()
        with self.store.lock, self.store.db() as db:
            row = db.execute("SELECT * FROM sticker_library WHERE id=?", (identifier,)).fetchone()
            check(row is not None and row["enabled"], "sticker_disabled", "表情已停用或移除。")
            check(row["revision"] == expected, "sticker_changed", "表情已改变，本次选择已失效。")
            content = self._decode(row)
            guard()
            return content

    def _decode(self, row):
        content = self.store.cipher.decrypt(bytes(row["body"]), "stickers", row["id"], row["sha256"])
        check(
            len(content) == row["size"] and hashlib.sha256(content).hexdigest() == row["sha256"],
            "storage_integrity",
            "表情原件完整性校验失败。",
        )
        return content


def verify_stickers(store):
    library = StickerLibrary(store)
    with store.db() as db, db.stream("SELECT * FROM sticker_library") as rows:
        for row in rows:
            summary = library._summary(row)
            describe(summary["name"], summary["tags"])
            actual = inspect(library._decode(row))
            check(
                all(summary[key] == actual[key] for key in actual), "backup_invalid", "备份表情资料不一致。"
            )
