"""Read the sealed snapshots of one live content record in the desktop database."""
import re

from .errors import AssistantError

KINDS = {"memories", "knowledge", "diaries", "conversation_summaries", "interaction_growth"}
VERSION = re.compile(r"[0-9a-f]{64}")


def _head(db, kind, identifier):
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
        raise AssistantError("invalid_query", "资料标识无效")
    row = db.execute("SELECT version,updated_at FROM content_records WHERE kind=? AND id=?",
                     (kind, identifier)).fetchone()
    if not row:
        raise AssistantError("not_found", "找不到这份资料")
    return row


def read(store, kind, identifier, version):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise AssistantError("invalid_query", "资料版本无效")
    with store.db() as db:
        head = _head(db, kind, identifier)
        table = "content_records" if version == head["version"] else "content_history"
        row = db.execute(f"SELECT body FROM {table} WHERE kind=? AND id=? AND version=?",
                         (kind, identifier, version)).fetchone()
        if not row:
            raise AssistantError("not_found", "找不到这个资料版本")
        return store._decode(kind, identifier, version, row["body"])


def page(store, kind, identifier, *, after=None, limit=20):
    if type(limit) is not int or not 1 <= limit <= 50 or not (after is None or isinstance(after, str)):
        raise AssistantError("invalid_query", "资料版本分页参数无效")
    with store.db() as db:
        head = _head(db, kind, identifier)
        current = head["version"]
        args = [kind, identifier, current]
        anchor = ""
        if after:
            cursor = after.split(":")
            if len(cursor) != 2 or cursor[0] != current or not VERSION.fullmatch(cursor[1]):
                raise AssistantError("edit_conflict", "版本列表已变化，请重新读取")
            if cursor[1] != current:
                row = db.execute("SELECT archived_at FROM content_history WHERE kind=? AND id=? AND version=?",
                                 (kind, identifier, cursor[1])).fetchone()
                if not row:
                    raise AssistantError("edit_conflict", "版本列表已变化，请重新读取")
                anchor = " AND (archived_at,version)<(?,?)"
                args.extend((row["archived_at"], cursor[1]))
        original = db.execute("SELECT version FROM content_history WHERE kind=? AND id=? "
                              "ORDER BY archived_at,version LIMIT 1", (kind, identifier)).fetchone()
        rows = db.execute("SELECT version,archived_at FROM content_history WHERE kind=? AND id=? AND version<>?"
                          + anchor + " ORDER BY archived_at DESC,version DESC LIMIT ?", (*args, limit + 1)).fetchall()
        items = [] if after else [{"version": current, "at": head["updated_at"].isoformat(), "current": True}]
        items.extend({"version": row["version"], "at": row["archived_at"].isoformat(), "current": False} for row in rows)
        return {"current": current, "original": original["version"] if original else current, "items": items[:limit],
                "next": current + ":" + items[limit - 1]["version"] if len(items) > limit else None}
