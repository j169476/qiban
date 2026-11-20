"""Edit the same summaries and private growth that subsequent replies read."""
from datetime import datetime, timezone

from companion_core import record_versions
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError

FIELDS = {
    "context.source_list": {"layer", "after"},
    "context.source_detail": {"layer", "id"},
    "context.source_save": {"layer", "id", "expected", "fields"},
    "context.source_versions": {"layer", "id", "after"},
    "context.source_version": {"layer", "id", "version"},
}
PAGE = 20
GROWTH = "interaction_growth"
SUMMARY = "conversation_summaries"
GROWTH_FIELDS = {"reflection": "", "traits": [], "speech_habits": [], "opinions": [], "interests": {}}


def invalid():
    raise AssistantError("invalid_query", "资料请求无效")


def growth_scope(data):
    return stable_key("private-growth", data["account"], data["actor"], data["conversation"])


def conversation_label(store, layer, data):
    platform_names = {"local": "会客室", "qq": "QQ", "wechat": "微信", "bili_private": "B 站", "bilibili": "B 站"}
    if layer == "summary":
        ref = data["turn_refs"][0]
        if store.exists("conversation_turns", ref["id"]):
            turn = store.read("conversation_turns", ref["id"])["data"]
            if turn["thread_id"] == data["thread_id"] and turn["account_uid"] == data["account_uid"]:
                return " · ".join((platform_names.get(turn["channel"], turn["channel"]),
                                   turn["sender_uid"] or turn["thread_key"], data["account_uid"]))
        return data["account_uid"] + " · " + data["source"]["recorded_at"][:10]
    with store.db() as db:
        row = db.execute("SELECT identity_body FROM actor_nodes WHERE actor_key=?", (data["actor"],)).fetchone()
    actor = store.ledger_decode("actor_nodes.identity", row[0])["user_id"] if row else data["actor"]
    return " · ".join((platform_names.get(data["channel"], data["channel"]), actor, data["account"]))


def describe(store, layer, identifier, item):
    data = item["data"]
    if layer == "summary":
        fields = {"content": data["content"]}
        version = item["version"]
    else:
        fields = {k: data["state"].get(k, default) for k, default in GROWTH_FIELDS.items()}
        version = data["id"] + ":" + item["version"]
    return {"id": identifier, "title": conversation_label(store, layer, data), "at": data["source"]["recorded_at"],
            "fields": fields, "version": version}


def source(store, layer, identifier):
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
        invalid()
    if layer == "summary":
        return store.read(SUMMARY, identifier)
    if not identifier.startswith("private-growth-"):
        invalid()
    head = store.state(identifier)
    if not head:
        raise AssistantError("not_found", "找不到该会话的成长记录")
    item = store.read(GROWTH, head)
    if growth_scope(item["data"]) != identifier:
        invalid()
    return item


def listing(store, layer, after):
    if after is not None and (not isinstance(after, str) or len(after) > 200):
        invalid()
    with store.lock, store.db() as db:
        if layer == "summary":
            rows = db.execute("SELECT id FROM content_records WHERE kind=? AND id>? ORDER BY id LIMIT ?",
                              (SUMMARY, after or "", PAGE + 1)).fetchall()
        else:
            rows = db.execute("SELECT key FROM state WHERE key LIKE 'private-growth-%' AND key>? ORDER BY key LIMIT ?",
                              (after or "", PAGE + 1)).fetchall()
        items = []
        for (identifier,) in rows[:PAGE]:
            try:
                item = describe(store, layer, identifier, source(store, layer, identifier))
            except AssistantError as exc:
                if exc.code == "not_found" and layer == "growth":
                    continue  # The conversation's head was deleted.
                raise
            fields = item.pop("fields")
            item["preview"] = (fields.get("content") or fields.get("reflection") or
                               "、".join(fields.get("traits", []) or fields.get("interests", {})))[:120]
            items.append(item)
        return {"items": items, "after": rows[PAGE - 1][0] if len(rows) > PAGE else None}


def growth_snapshot(store, scope, version):
    if not isinstance(version, str) or len(version) > 280 or ":" not in version:
        invalid()
    record_id, digest = version.split(":", 1)
    item = record_versions.read(store, GROWTH, record_id, digest)
    if growth_scope(item["data"]) != scope:
        raise AssistantError("not_found", "找不到此版本")
    return describe(store, "growth", scope, item)


def growth_history(store, scope, current, after):
    # Only manual edits add entries. Ordinary replies may advance the conversation head.
    prefix = "context-growth-edits:" + scope
    count = store.state(prefix, 0)
    if after is not None and (not isinstance(after, str) or not after.isascii() or not after.isdigit()
                              or len(after) > 18 or not 1 <= int(after) <= count):
        invalid()
    items = [] if after else [{"version": current["version"], "at": current["at"], "current": True}]
    index = int(after) if after else count
    while index > 0 and len(items) < PAGE:
        saved = store.state(f"{prefix}:{index}")
        if saved["version"] != current["version"]:
            items.append({**saved, "current": False})
        index -= 1
    original = store.state(prefix + ":1") if count else current
    return {"current": current["version"], "original": original["version"], "items": items,
            "next": str(index) if index else None}


def remember_growth_edit(store, scope, before, after, db):
    prefix = "context-growth-edits:" + scope
    count = store.state(prefix, 0, db)
    last = store.state(f"{prefix}:{count}", {}, db)
    at = datetime.now(timezone.utc).isoformat()
    for snapshot in (before, after):
        if snapshot["version"] != last.get("version"):
            count += 1
            last = {"version": snapshot["version"], "at": at}
            store.set_state(f"{prefix}:{count}", last, db)
    store.set_state(prefix, count, db)


def operate(manager, request, op, args):
    layer = args["layer"]
    if layer not in ("summary", "growth"):
        invalid()
    store = manager.store
    if op == "context.source_list":
        return listing(store, layer, args["after"])
    identifier = args["id"]
    with store.lock, store.db() as db:
        if op == "context.source_save":
            db.execute("BEGIN IMMEDIATE")
        manager.authority.require(request, administrator=True)
        item = source(store, layer, identifier)
        current = describe(store, layer, identifier, item)
        if op == "context.source_detail":
            return current
        if op == "context.source_versions":
            if layer == "summary":
                return record_versions.page(store, SUMMARY, identifier, after=args["after"], limit=PAGE)
            return growth_history(store, identifier, current, args["after"])
        if op == "context.source_version":
            if layer == "summary":
                return describe(store, layer, identifier, record_versions.read(store, SUMMARY, identifier, args["version"]))
            return growth_snapshot(store, identifier, args["version"])
        if op != "context.source_save":
            invalid()
        if args["expected"] != current["version"]:
            raise AssistantError("edit_conflict", "资料已更新，请重新读取")
        fields = args["fields"]
        if not isinstance(fields, dict) or fields.keys() != current["fields"].keys():
            invalid()
        if fields == current["fields"]:
            return current
        data = item["data"].copy()
        if layer == "summary":
            if not isinstance(fields["content"], str) or not fields["content"].strip():
                raise AssistantError("workspace_config", "摘要不能为空")
            data.update(fields)
        else:
            data["state"] = {**data["state"], **fields}
        saved = manager.records.correct(request, SUMMARY if layer == "summary" else GROWTH,
                                        data["id"], data, item["version"])
        updated = describe(store, layer, identifier, saved)
        if layer == "growth":
            remember_growth_edit(store, identifier, current, updated, db)
        return updated
