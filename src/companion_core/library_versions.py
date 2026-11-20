"""Versioned originals for the desktop file and sticker libraries."""
import hashlib
import secrets
import time
from datetime import datetime, timezone

from .errors import AssistantError
from .file_library import FileLibrary
from .governance import GovernanceRepository
from .media_originals import MediaOriginals
from .sticker_library import StickerLibrary, inspect

LIBRARIES = {
    "files": ("file-library", "file_library", "file-library:", FileLibrary),
    "stickers": ("stickers", "sticker_library", "sticker:", StickerLibrary),
}
PAGE_SIZE = 4


class LibraryVersions:
    def __init__(self, store, layer):
        if not isinstance(layer, str) or layer not in LIBRARIES:
            raise AssistantError("management_request", "请选择文件或表情。")
        self.store, self.layer = store, layer
        self.kind, self.table, self.prefix, library = LIBRARIES[layer]
        self.library = library(store)

    def _head(self, db, identifier, *, lock=False):
        row = db.execute("SELECT * FROM " + self.table + " WHERE id=?" + (" FOR UPDATE" if lock else ""),
                         (identifier,)).fetchone()
        if row is None:
            raise AssistantError("not_found", "这份资料已不存在。")
        return row

    def archive(self, db, row, *, changing_body=False, incoming_bytes=0):
        """Keep the outgoing editable version in the same transaction as its replacement."""
        metadata = self.store.ledger_decode(self.prefix + row["id"], row["metadata"])
        metadata.pop("scopes", None)
        envelope = {"kind": self.kind, "id": row["id"], "sha256": row["sha256"], "data": metadata}
        sealed = self.store.ledger_encode("library-version:" + row["revision"], envelope)
        # An existing history body with matching current digest is moved back to active
        # on restore. No archived body is kept for the active digest.
        MediaOriginals(self.store)._make_room(db, len(sealed) + incoming_bytes, lambda: None)
        if changing_body:
            db.execute("INSERT INTO library_version_bodies VALUES(?,?,?,?,?)",
                       (self.kind, row["id"], row["sha256"], row["size"], row["body"]))
        db.execute("""INSERT INTO library_versions(kind,id,position,revision,sha256,size,metadata,archived_at)
                      SELECT ?,?,COALESCE(MAX(position),0)+1,?,?,?,?,? FROM library_versions WHERE kind=? AND id=?""",
                   (self.kind, row["id"], row["revision"], row["sha256"], row["size"], sealed,
                    time.time(), self.kind, row["id"]))

    def _metadata(self, row):
        envelope = self.store.ledger_decode("library-version:" + row["revision"], row["metadata"])
        if (envelope.get("kind"), envelope.get("id"), envelope.get("sha256")) != (
                self.kind, row["id"], row["sha256"]):
            raise AssistantError("storage_integrity", "资料版本校验失败。")
        return envelope["data"]

    def _historical(self, db, identifier, revision):
        row = db.execute("SELECT * FROM library_versions WHERE kind=? AND id=? AND revision=?",
                         (self.kind, identifier, revision)).fetchone()
        if row is None:
            raise AssistantError("not_found", "找不到该资料版本。")
        return row

    def _body(self, db, head, digest):
        if head["sha256"] == digest:
            return self.library._decode(head)
        row = db.execute("SELECT * FROM library_version_bodies WHERE kind=? AND id=? AND sha256=?",
                         (self.kind, head["id"], digest)).fetchone()
        if row is None:
            raise AssistantError("storage_integrity", "缺少此版本的原件。")
        return self.library._decode(row)

    def listing(self, identifier, after):
        if after is not None and (not isinstance(after, str) or not after.isascii()
                                  or not after.isdigit() or len(after) > 18):
            raise AssistantError("management_request", "请重新打开版本列表。")
        with self.store.lock, self.store.db() as db:
            head = self._head(db, identifier)
            args = (self.kind, identifier)
            rows = db.execute("SELECT * FROM library_versions WHERE kind=? AND id=?" +
                              (" AND position<?" if after is not None else "") +
                              " ORDER BY position DESC LIMIT ?",
                              (*args, *((int(after),) if after is not None else ()), PAGE_SIZE + 1)).fetchall()
            items = []
            if after is None:
                at = rows[0]["archived_at"] if rows else head["created_at"]
                items.append(self._entry(head, self.library._summary(head), at, True))
            for row in rows[:PAGE_SIZE]:
                items.append(self._entry(row, self._metadata(row), row["archived_at"], False))
            return {"items": items, "after": str(rows[PAGE_SIZE - 1]["position"]) if len(rows) > PAGE_SIZE else None}

    @staticmethod
    def _entry(row, metadata, timestamp, current):
        entry = {key: metadata[key] for key in ("name", "tags", "format", "preview") if key in metadata}
        return {**entry, "revision": row["revision"], "sha256": row["sha256"], "bytes": row["size"],
                "current": current, "at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}

    def read_version(self, identifier, revision):
        with self.store.lock, self.store.db() as db:
            head = self._head(db, identifier)
            source = head if revision == head["revision"] else self._historical(db, identifier, revision)
            return self._body(db, head, source["sha256"])

    def info(self, identifier, revision):
        with self.store.lock, self.store.db() as db:
            head = self._head(db, identifier)
            if revision == head["revision"]:
                return self._entry(head, self.library._summary(head), head["created_at"], True)
            row = self._historical(db, identifier, revision)
            return self._entry(row, self._metadata(row), row["archived_at"], False)

    def save(self, identifier, expected, *, actor, content=None, revision=None):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._head(db, identifier, lock=True)
            if head["revision"] != expected:
                raise AssistantError("edit_conflict", "资料已更新，请重新读取。")
            metadata = self.store.ledger_decode(self.prefix + identifier, head["metadata"])
            if revision is not None:
                if revision == expected:
                    return self.library._summary(head)
                source = self._historical(db, identifier, revision)
                old = self._metadata(source)
                target = {**metadata, **old}
                content = self._body(db, head, source["sha256"])
            else:
                if not isinstance(content, bytes) or not 0 < len(content) <= 10 * 1024 * 1024:
                    raise AssistantError("invalid_data", "原件须非空且不超过 10 MiB。")
                target = {**metadata, **(inspect(content) if self.layer == "stickers" else {})}
            digest = hashlib.sha256(content).hexdigest()
            changed = digest != head["sha256"]
            if not changed and target == metadata:
                return self.library._summary(head)
            if changed:
                db.execute("DELETE FROM library_version_bodies WHERE kind=? AND id=? AND sha256=?",
                           (self.kind, identifier, digest))
            # With an archived target removed, both restoring it and introducing a new
            # body add len(content) to the current accounting. Failure rolls back the swap.
            self.archive(db, head, changing_body=changed, incoming_bytes=len(content) if changed else 0)
            next_version = secrets.token_hex(16)
            body = self.store.cipher.encrypt(content, self.kind, identifier, digest) if changed else head["body"]
            db.execute("UPDATE " + self.table + " SET sha256=?, size=?, metadata=?, body=?, revision=? WHERE id=?",
                       (digest, len(content), self.store.ledger_encode(self.prefix + identifier, target),
                        body, next_version, identifier))
            GovernanceRepository(self.store).audit(actor, "library_restored" if revision else "library_replaced",
                                                  self.prefix + identifier, expected, next_version, db=db)
            return self.library.get(identifier, db=db)


def verify_versions(store):
    from .file_library import describe as file_metadata
    from .sticker_library import describe as sticker_metadata

    with store.db() as db:
        for layer, descriptor in (("files", file_metadata), ("stickers", sticker_metadata)):
            versions = LibraryVersions(store, layer)
            # Every archived revision must resolve to exactly one original (active or
            # archived), owned by the same item and of the recorded length.
            mismatch = db.execute("""SELECT 1 FROM library_versions h LEFT JOIN """ + versions.table + """ a
                ON a.id=h.id LEFT JOIN library_version_bodies b ON b.kind=h.kind AND b.id=h.id AND b.sha256=h.sha256
                WHERE h.kind=? AND (a.id IS NULL OR h.revision=a.revision OR
                CASE WHEN a.sha256=h.sha256 THEN b.id IS NOT NULL OR h.size<>a.size
                     ELSE b.id IS NULL OR h.size<>b.size END) LIMIT 1""", (versions.kind,)).fetchone()
            orphan = db.execute("""SELECT 1 FROM library_version_bodies b WHERE b.kind=? AND NOT EXISTS
                (SELECT 1 FROM library_versions h WHERE h.kind=b.kind AND h.id=b.id AND h.sha256=b.sha256)
                LIMIT 1""", (versions.kind,)).fetchone()
            if mismatch or orphan:
                raise AssistantError("backup_invalid", "资料版本与原件不一致。")
            with db.stream("SELECT * FROM library_versions WHERE kind=?", (versions.kind,)) as rows:
                for row in rows:
                    data = versions._metadata(row)
                    descriptor(data.get("name"), data.get("tags"))
                    required = {"name", "tags"}
                    if layer == "stickers":
                        required |= {"format", "width", "height", "frames", "preview"}
                    if set(data) != required:
                        raise AssistantError("backup_invalid", "资料版本字段不一致。")
            with db.stream("SELECT * FROM library_version_bodies WHERE kind=?", (versions.kind,), batch=1) as rows:
                for row in rows:
                    content = versions.library._decode(row)
                    if layer == "stickers":
                        inspect(content)
