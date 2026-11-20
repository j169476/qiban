"""Named reply settings stored in the same encrypted database as the active workspace."""
import re
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from companion_core import context_workspace as workspace
from companion_core.errors import AssistantError

FIELDS = {
    "context.presets": {"after"},
    "context.preset_read": {"id"},
    "context.preset_save": {"id", "revision", "name", "config"},
    "context.preset_apply": {"id", "revision", "base"},
    "context.preset_replies": {"id", "after"},
}

PREFIX = workspace.PRESETS
PAGE_SIZE = 25


def key(identifier):
    if not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier):
        raise AssistantError("workspace_preset", "方案标识无效")
    return PREFIX + identifier


def read(store, identifier, db=None):
    if identifier == "default":
        return {"id": "default", "name": "默认", "revision": "initial", "config": deepcopy(workspace.DEFAULT)}
    preset = store.state(key(identifier), db=db)
    if preset is None:
        raise AssistantError("workspace_preset", "找不到这个方案")
    return preset


def listing(store, after):
    start = PREFIX if after is None else key(after)
    with store.db() as db:
        rows = db.execute("SELECT key,value FROM state WHERE key LIKE ? AND key>? ORDER BY key LIMIT ?",
                          (PREFIX + "%", start, PAGE_SIZE + 1)).fetchall()
    items = []
    for row in rows[:PAGE_SIZE]:
        preset = store.ledger_decode("state:" + row["key"], row["value"])
        items.append({field: preset[field] for field in ("id", "name", "revision")})
    return {"items": items, "after": items[-1]["id"] if len(rows) > PAGE_SIZE else None}


def save(store, db, args):
    preset_key = key(args["id"])
    name = args["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not name.strip().isprintable():
        raise AssistantError("workspace_preset", "方案名称须为 1 至 80 个可显示字符")
    prior = store.state(preset_key, db=db)
    if args["revision"] != (prior["revision"] if prior else None):
        raise AssistantError("edit_conflict", "方案已变更，请重新读取")
    name = name.strip()
    if prior and prior["name"] == name and prior["config"] == args["config"]:
        return prior
    preset = {"id": args["id"], "name": name, "config": args["config"], "revision": uuid4().hex}
    store.set_state(preset_key, preset, db)
    return preset


def replies(store, identifier, after):
    read(store, identifier)
    params = [identifier]
    cursor_sql = ""
    if after is not None:
        try:
            at, item_id = after.split("|")
            stamp = datetime.fromisoformat(at)
            if stamp.tzinfo is None or not re.fullmatch(r"turn-[a-f0-9]{32}", item_id):
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise AssistantError("invalid_query", "回复记录页码无效") from None
        cursor_sql = " AND (r.sent_at,r.id)<(?,?)"
        params.extend((stamp, item_id))
    with store.db() as db:
        rows = db.execute("SELECT r.id,r.sent_at,c.version,c.body FROM preset_reply_turns r "
                          "JOIN content_records c USING(kind,id) WHERE r.preset_id=?" + cursor_sql +
                          " ORDER BY r.sent_at DESC,r.id DESC LIMIT 21", params).fetchall()
        items = []
        for row in rows[:20]:
            doc = store._decode("conversation_turns", row["id"], row["version"], row["body"])["data"]
            platform = {"dm": "bili_private", "comment": "bilibili"}.get(doc["channel"], doc["channel"])
            items.append({"id": row["id"], "at": row["sent_at"].isoformat(), "platform": platform,
                          "text": doc["content"]})
    cursor = items[-1]["at"] + "|" + items[-1]["id"] if len(rows) > 20 else None
    return {"items": items, "after": cursor}


def operate(manager, request, op, args):
    store = manager.store

    def authorize():
        manager.authority.require(request, administrator=True)

    if op == "context.presets":
        result = listing(store, args["after"])
    elif op == "context.preset_read":
        result = read(store, args["id"])
    elif op == "context.preset_replies":
        result = replies(store, args["id"], args["after"])
    else:
        with workspace.editing(store, args, authorize) as (db, view):
            if op == "context.preset_save":
                workspace.validate_effective(store, args["config"])
                preset = save(store, db, args)
            else:
                preset = read(store, args["id"], db)
                if preset["revision"] != args["revision"]:
                    raise AssistantError("edit_conflict", "方案已变更，请重新读取")
                view = workspace.apply_preset(store, db, view, preset, request.actor)
            authorize()
            result = {"preset": preset, "workspace": view}
    return result
