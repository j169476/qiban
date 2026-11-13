"""D30/D31 scoped file choices and independent outgoing limits."""
import secrets
from dataclasses import asdict

from companion_core.errors import AssistantError
from companion_core.file_library import FileLibrary, describe
from companion_core.governance import GovernanceRepository
from companion_core.media_originals import MediaOriginals
from companion_core.reply_authorization import ReplyAuthorizations, require

KEY = "qq-file-reply-settings-v1"


class FileReplySettings:
    def __init__(self, store):
        self.store = store

    def read(self):
        return self.store.state(KEY, {"count": 3, "total_mib": 20, "revision": None})

    def configure(self, count, total_mib, expected, *, actor):
        if type(count) is not int or not 1 <= count <= 12 or type(total_mib) is not int or not 1 <= total_mib <= 120:
            raise AssistantError("qq_file_limits", "每次文件数须为1至12，总大小须为1至120 MiB的整数。")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self.read()
            require(old["revision"] == expected, "edit_conflict")
            value = {"count": count, "total_mib": total_mib, "revision": secrets.token_hex(16)}
            self.store.set_state(KEY, value, db)
            GovernanceRepository(self.store).audit(actor, "qq_file_limits_changed", KEY,
                                                  expected, value["revision"], db=db)
        return value


class ReplyFiles:
    def __init__(self, store):
        self.store = store
        self.library, self.originals = FileLibrary(store), MediaOriginals(store)

    def candidates(self, scope, authorization, text):
        items = [{**item, "id": "library:" + item["id"]} for item in
                 self.library.candidates(scope, authorization, text)]
        with self.store.lock, self.store.db() as db:
            ReplyAuthorizations(self.store).check(scope, authorization, db=db)
            rows = db.execute("SELECT id,scope,event_id,source_version,revision,metadata,size,status,processed_at,created_at FROM media_originals WHERE scope=? AND status='available' ORDER BY created_at DESC,id LIMIT 100",
                              (scope.id,)).fetchall()
            originals = []
            for row in rows:
                item = self.originals._summary(row)
                if item["kind"] != "file":
                    continue
                try:
                    self.originals._source(scope, row["event_id"], row["source_version"], row["id"])
                    describe(item["name"], [])
                except AssistantError as error:
                    if error.code not in {"media_source_changed", "file_library_invalid"}:
                        raise
                    continue
                originals.append({"id": "attachment:" + item["id"], "revision": item["revision"],
                                  "name": item["name"], "bytes": item["bytes"], "tags": []})
                if len(originals) == 12:
                    break
        return {"items": originals + items, "limits": FileReplySettings(self.store).read(),
                "guidance": "可单独发送文件，也可附在文字或语音后、表情前。仅选本轮ID；附件限当前会话。名称标签不等于已阅读正文。"}

    def check(self, scope, authorization, item):
        ReplyAuthorizations(self.store).check(scope, authorization)
        source, identifier = item["id"].split(":", 1)
        if source == "library":
            current = self.library.get(identifier)
            require(current["enabled"] and asdict(scope) in current["scopes"], "file_library_disabled")
        else:
            require(source == "attachment", "qq_file_not_offered")
            with self.store.db() as db:
                row = db.execute("SELECT id,scope,event_id,source_version,revision,metadata,size,status,processed_at,created_at FROM media_originals WHERE id=?", (identifier,)).fetchone()
                require(row is not None and row["scope"] == scope.id and row["status"] == "available", "media_expired")
                self.originals._source(scope, row["event_id"], row["source_version"], identifier)
                current = self.originals._summary(row)
                require(current["kind"] == "file", "qq_file_not_offered")
        require(all(current[key] == item[key] for key in ("revision", "name", "bytes")), "qq_file_changed")
        describe(current["name"], [])
        require(0 < current["bytes"] <= 10 * 1024 * 1024, "qq_file_limits")

    def validate(self, scope, selection):
        require(FileReplySettings(self.store).read() == selection["limits"], "qq_file_settings_changed")
        items = selection["items"]
        require(0 < len(items) <= selection["limits"]["count"] and
                sum(item["bytes"] for item in items.values()) <= selection["limits"]["total_mib"] * 1024 * 1024,
                "qq_file_limits")
        for item in items.values():
            self.check(scope, selection["authorization"], item)

    def read(self, scope, selection, identifier, *, guard):
        self.check(scope, selection["authorization"], selection["items"][identifier])
        source, key = identifier.split(":", 1)
        if source == "library":
            return self.library.read_for_send(scope, selection["authorization"], key,
                selection["items"][identifier]["revision"], guard=guard)
        return self.originals.read(scope, selection["authorization"], key, guard=guard)
