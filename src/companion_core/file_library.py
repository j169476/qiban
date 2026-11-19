"""Administrator-imported originals with explicit per-account conversation grants."""

import hashlib
import re
import secrets
import time
from dataclasses import asdict
from pathlib import Path

from .errors import AssistantError
from .governance import GovernanceRepository
from .reply_authorization import ReplyAuthorizations, ReplyScope

MAX_BYTES = 10 * 1024 * 1024


def check(ok, code="file_library_invalid", message="文件资料无效，请重新读取。"):
    if not ok:
        raise AssistantError(code, message)


def describe(name, tags):
    check(isinstance(name, str) and 0 < len(name) <= 180
          and not re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
          and not name.endswith((".", " "))
          and not re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", name),
          message="请使用有效文件名（含扩展名，最多180个字符）。")
    check(isinstance(tags, list) and len(tags) <= 20
          and all(isinstance(tag, str) and 0 < len(tag.strip()) <= 40 for tag in tags),
          message="最多20个标签，每个标签1至40个字符。")
    return {"name": name, "tags": list(dict.fromkeys(tag.strip() for tag in tags))}


def scopes(values):
    check(isinstance(values, list) and len(values) <= 1000)
    result = {}
    for value in values:
        check(isinstance(value, dict) and set(value) == {"platform", "account_identity", "kind", "target_id"})
        scope = ReplyScope(**value)
        check(scope.platform == "qq", message="当前文件发送支持QQ会话。")
        result[scope.id] = asdict(scope)
    return [result[key] for key in sorted(result)]


class FileLibrary:
    def __init__(self, store):
        self.store = store

    def import_file(self, path, tags, *, actor):
        check(isinstance(path, str) and Path(path).is_absolute(), message="请通过文件选择器选择文件。")
        describe(Path(path).name, tags)
        try:
            with Path(path).open("rb") as stream:
                content = stream.read(MAX_BYTES + 1)
        except OSError:
            raise AssistantError("file_library_read", "无法读取所选文件，请重新选择。") from None
        return self.import_bytes(content, Path(path).name, tags, actor=actor)

    def import_bytes(self, content, name, tags, *, actor):
        metadata = {**describe(name, tags), "scopes": []}
        check(isinstance(content, bytes) and 0 < len(content) <= MAX_BYTES,
              message="文件须非空且不超过10 MiB。")
        digest = hashlib.sha256(content).hexdigest()
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM file_library WHERE sha256=?", (digest,)).fetchone()
            if old:
                return {"item": self._summary(old), "duplicate": True}
            from .media_originals import MediaOriginals
            MediaOriginals(self.store)._make_room(db, len(content), lambda: None)
            identifier, revision = secrets.token_hex(16), secrets.token_hex(16)
            db.execute("INSERT INTO file_library VALUES(?,?,?,?,?,?,?,?)", (
                identifier, digest, len(content), self.store.ledger_encode("file-library:" + identifier, metadata),
                self.store.cipher.encrypt(content, "file-library", identifier, digest), revision, False, time.time()))
            GovernanceRepository(self.store).audit(actor, "file_imported", "file-library:" + identifier,
                                                   None, revision, db=db)
            return {"item": self.get(identifier, db=db), "duplicate": False}

    def _summary(self, row):
        metadata = self.store.ledger_decode("file-library:" + row["id"], row["metadata"])
        return {**metadata, "id": row["id"], "revision": row["revision"], "enabled": row["enabled"],
                "bytes": row["size"], "sha256": row["sha256"]}

    def get(self, identifier, *, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.get(identifier, db=connection)
        row = db.execute("SELECT * FROM file_library WHERE id=?", (identifier,)).fetchone()
        check(row is not None, "file_library_missing", "文件不存在或已移除。")
        return self._summary(row)

    def listing(self, after="", limit=5):
        check(isinstance(after, str) and len(after) <= 128 and type(limit) is int and 1 <= limit <= 5)
        with self.store.db() as db:
            return {"items": [self._summary(row) for row in db.execute(
                "SELECT * FROM file_library WHERE id>? ORDER BY id LIMIT ?", (after, limit))]}

    def targets(self):
        return {"items": [record for record in ReplyAuthorizations(self.store).records()
                          if record["enabled"] and record["scope"]["platform"] == "qq"]}

    def configure(self, identifier, name, tags, enabled, allowed_scopes, expected, *, actor):
        values = {**describe(name, tags), "scopes": scopes(allowed_scopes)}
        check(type(enabled) is bool)
        check(not enabled or values["scopes"], message="启用前请选择至少一个可使用的会话。")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM file_library WHERE id=? FOR UPDATE", (identifier,)).fetchone()
            check(row is not None, "file_library_missing")
            check(row["revision"] == expected, "edit_conflict", "文件设置已改变，请重新读取。")
            if enabled:
                for value in values["scopes"]:
                    check(ReplyAuthorizations(self.store).version(ReplyScope(**value), db=db) is not None,
                          "reply_not_authorized", "所选会话已停止授权，请重新选择。")
            revision = secrets.token_hex(16)
            db.execute("UPDATE file_library SET metadata=?,enabled=?,revision=? WHERE id=?", (
                self.store.ledger_encode("file-library:" + identifier, values), enabled, revision, identifier))
            GovernanceRepository(self.store).audit(actor, "file_changed", "file-library:" + identifier,
                                                   expected, revision, db=db)
            return self.get(identifier, db=db)

    def candidates(self, scope, authorization, text, *, limit=12):
        check(isinstance(text, str) and type(limit) is int and 1 <= limit <= 50)
        ranked = []
        with self.store.lock, self.store.db() as db:
            ReplyAuthorizations(self.store).check(scope, authorization, db=db)
            for row in db.execute("SELECT id,sha256,size,metadata,revision,enabled FROM file_library WHERE enabled ORDER BY id"):
                item = self._summary(row)
                if asdict(scope) not in item["scopes"]:
                    continue
                score = sum(word.casefold() in text.casefold() for word in [item["name"], *item["tags"]])
                ranked.append((-score, item["id"], {key: item[key] for key in ("id", "revision", "name", "tags", "bytes")}))
        return [item for _, _, item in sorted(ranked)[:limit]]

    def read_for_send(self, scope, authorization, identifier, expected, *, guard):
        with self.store.lock, self.store.db() as db:
            guard()
            ReplyAuthorizations(self.store).check(scope, authorization, db=db)
            row = db.execute("SELECT * FROM file_library WHERE id=?", (identifier,)).fetchone()
            check(row is not None and row["enabled"], "file_library_disabled", "文件已停用或移除。")
            check(row["revision"] == expected, "file_library_changed", "文件设置已改变，本次选择已失效。")
            item = self._summary(row)
            check(asdict(scope) in item["scopes"], "access_denied", "当前会话无权使用此文件。")
            content = self._decode(row)
            guard()
            return content

    def _decode(self, row):
        content = self.store.cipher.decrypt(bytes(row["body"]), "file-library", row["id"], row["sha256"])
        check(len(content) == row["size"] and hashlib.sha256(content).hexdigest() == row["sha256"],
              "storage_integrity", "文件原件完整性校验失败。")
        return content


def verify_files(store):
    library = FileLibrary(store)
    with store.db() as db, db.stream("SELECT * FROM file_library") as rows:
        for row in rows:
            item = library._summary(row)
            describe(item["name"], item["tags"])
            check(scopes(item["scopes"]) == item["scopes"], "backup_invalid")
            check(not item["enabled"] or item["scopes"], "backup_invalid")
            library._decode(row)
