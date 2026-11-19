"""Bilibili learning goals in the desktop workbench. Notes inherit their goal's access."""
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from companion_core.cognition import stable_id
from companion_core.errors import AssistantError
from companion_core.learning import definition
from companion_core.learning_tree import assessment_current, graph

SCOPE = {"account", "goal", "definition"}
FIELDS = {
    "learning.index_status": set(), "learning.index_control": {"paused"},
    "learning.goals": {"after"}, "learning.snapshot": SCOPE,
    "learning.search": SCOPE | {"snapshot", "query", "filter", "offset"},
    "learning.tile": SCOPE | {"snapshot", "x", "y", "level", "offset"},
    "learning.detail": SCOPE | {"snapshot", "node"},
    "learning.edit": SCOPE | {"snapshot", "node", "expected", "changes"},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def invalid():
    raise AssistantError("management_request", "学习树参数无效")


def unavailable():
    raise AssistantError("learning_unavailable", "目标不可用，请重新读取")


def criterion_versions(manager, request, goal, signature):
    store = manager.store
    ids = [stable_id("node", f"{goal['data']['id']}:{signature}:{i}")
           for i in range(len(goal["data"]["criteria"]))]
    visible = manager.repository.readable_resources(manager.admin.access, request,
                                                    candidates=["learning_nodes/" + key for key in ids])
    with store.db() as db:
        rows = db.execute("SELECT id,version FROM content_records WHERE kind='learning_nodes' AND id=ANY(?) ORDER BY id",
                          ([k for k in ids if "learning_nodes/" + k in visible],)).fetchall()
    return tuple((r["id"], r["version"]) for r in rows)


def strip(node):
    return {k: node[k] for k in ("id", "type", "version", "title", "parent_id", "prerequisites", "archived", "x", "y")}


def changed_notes(store, prefix, previous, version):
    if previous is None:
        return None
    cursor, rows = previous
    keys = set()
    for entry in store.state(prefix + ":changes", []):
        if entry["before"] == cursor:
            cursor = entry["after"]
            keys.add(entry["id"])
    if cursor != version:
        return None
    updated = rows.copy()
    moved = False
    for key in keys:
        note = store.state(prefix + ":note:" + key)
        if not note:
            return None
        before = rows.get(key)
        moved |= before is None or before["parent_id"] != note["parent_id"]
        coords = {k: before[k] for k in ("x", "y")} if before else {}
        updated[key] = {**note, **coords, "type": "note"}
    if moved:
        # Layout may move descendants. Never mutate a previously published view.
        updated = {k: v.copy() for k, v in updated.items()}
        layout(updated)
    return updated


def snapshot(manager, goal, prefix, versions, note_version):
    nodes, issues = manager.store.records_selected("learning_nodes", [k for k, _ in versions])
    existing = {n["data"]["id"]: n for n in nodes if n["data"]["goal_id"] == goal["data"]["id"]
                and n["data"]["account_uid"] == goal["data"]["account_uid"] and n["data"]["definition"] == definition(goal["data"])}
    if issues or tuple(sorted((k, v["version"]) for k, v in existing.items())) != versions:
        raise AssistantError("edit_conflict", "学习节点已发生变化")
    rows = {}
    for key, record in existing.items():
        data = record["data"]
        rows[key] = {"id": key, "type": "criterion", "version": record["version"], "title": data["title"],
                     "body": "", "parent_id": data["parent_id"], "prerequisites": data["prerequisites"],
                     "archived": False, "record": record}
    # Acquire a connection only for a bounded batch; rendering never takes the
    # reply writer's advisory lock. Reject a torn read before publishing it.
    from .desktop_learning_checkpoint import checkpoint
    signature = digest([prefix, versions, goal["version"], note_version])
    with checkpoint(manager.store, signature) as progress:
        append_notes(manager, prefix, rows, progress)
        layout(rows)
    return rows


def append_notes(manager, prefix, rows, progress):
    last = progress.resume(rows) if progress else ""
    after = prefix + ":note:" + last
    while True:
        if manager.learning_index.cancelled():
            raise AssistantError("index_obsolete", "学习视图已经关闭")
        if manager.learning_index.paused:
            raise AssistantError("index_paused", "学习索引已暂停")
        with manager.store.db() as db:
            entries = db.execute("SELECT key,value FROM state WHERE key LIKE ? AND key>? ORDER BY key LIMIT 128",
                                 (prefix + ":note:%", after)).fetchall()
        batch = []
        for entry in entries:
            note = manager.store.ledger_decode("state:" + entry["key"], entry["value"])
            rows[note["id"]] = dict(note, type="note")
            batch.append(note)
        if progress and entries:
            progress.append(rows, batch, force=manager.learning_index.paused)
        if len(entries) < 128:
            break
        after = entries[-1]["key"]


def layout(rows):
    # Stable traversal: a moved branch moves with its parent. A hidden parent
    # becomes a root; its identifier is never returned to the caller.
    children = {}
    for key, node in rows.items():
        if node["parent_id"] not in rows:
            node["parent_id"] = None
        node["prerequisites"] = [k for k in node["prerequisites"] if k in rows]
        children.setdefault(node["parent_id"], []).append(key)
    levels = {}
    stack = [(k, 0) for k in sorted(children.get(None, []), reverse=True)]
    while stack:
        key, depth = stack.pop()
        row = rows[key]
        row["x"], row["y"] = depth * 256, levels.get(depth, 0) * 96
        levels[depth] = levels.get(depth, 0) + 1
        stack.extend((k, depth + 1) for k in sorted(children.get(key, []), reverse=True))
    if sum(levels.values()) != len(rows):
        raise AssistantError("learning_graph", "目标中存在循环依赖")


def due(manager, goal, node):
    if node["type"] != "criterion":
        return False, False
    current = assessment_current(manager.store, goal, node["record"])
    at = node["record"]["data"]["next_review_at"]
    return current, bool(current and at and datetime.fromisoformat(at) <= datetime.now(timezone.utc))


def operate(manager, request, op, args):
    if op == "learning.index_status":
        return manager.learning_index.status()
    if op == "learning.index_control":
        if type(args["paused"]) is not bool:
            invalid()
        return manager.learning_index.control(args["paused"])
    broker = manager.bili
    if broker is None:
        raise AssistantError("bili_unavailable", "B 站账号服务尚未就绪")
    account = broker._account()
    if not account:
        raise AssistantError("bili_login_required", "请先登录 B 站")
    uid = account["uid"]
    store = manager.store
    scope = (uid, account["revision"])
    def guard():
        current = broker._account()
        if not current or (current["uid"], current["revision"]) != scope:
            raise AssistantError("account_changed", "B 站账号已切换")
        manager.authority.require(request, administrator=True)

    if op == "learning.goals":
        after = args["after"]
        if not isinstance(after, str) or len(after) > 150:
            invalid()
        items = []
        while len(items) < 51:
            with store.db() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM content_records WHERE kind='learning_goals' AND id>? ORDER BY id LIMIT 100",
                                                 (after,))]
            allowed = manager.repository.readable_resources(manager.admin.access, request,
                                                            candidates=["learning_goals/" + k for k in ids])
            selected, issues = store.records_selected("learning_goals", [k for k in ids if "learning_goals/" + k in allowed])
            if issues:
                raise AssistantError("learning_unavailable", "读取学习目标失败")
            for item in selected:
                row = item["data"]
                if row["account_uid"] == uid:
                    items.append({"id": row["id"], "title": row["objective"], "status": row["status"], "definition": definition(row)})
            if len(ids) < 100:
                break
            after = ids[-1]
        guard()
        return {"account": uid, "items": items[:50], "after": items[49]["id"] if len(items) > 50 else None}

    key = args["goal"]
    if not isinstance(key, str) or not 1 <= len(key) <= 150 or args["account"] != uid:
        unavailable()
    def target():
        if not manager.admin.access.can_read(request, "learning_goals/" + key):
            unavailable()
        goal = store.read("learning_goals", key)
        if goal["data"]["account_uid"] != uid:
            unavailable()
        if definition(goal["data"]) != args["definition"]:
            raise AssistantError("edit_conflict", "学习目标的定义已变更")
        return goal

    goal = target()
    prefix = "desktop-tree:" + digest([uid, key, args["definition"]])
    core_versions = criterion_versions(manager, request, goal, args["definition"])
    if op == "learning.edit":
        identifier = args["node"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identifier):
            invalid()
        receipt_key = prefix + ":receipt:" + identifier
        fingerprint = digest(args)
        ack = store.state(receipt_key, {})
        if ack.get("request") == fingerprint and (
                identifier.startswith("note-") or identifier in dict(core_versions)):
            guard()
            return ack["result"]
    def stamp():
        return store.state(prefix + ":revision", "initial")
    note_version = stamp()
    cache_key = (prefix, scope, request.session_id, request.epoch, goal["version"], tuple(core_versions), note_version)
    def build():
        rows = changed_notes(store, prefix, manager.learning_index.latest(cache_key), note_version)
        if rows is None:
            rows = snapshot(manager, goal, prefix, core_versions, note_version)
        if stamp() != note_version:
            raise AssistantError("edit_conflict", "构建期间笔记已经改变")
        return rows, digest([goal["version"], sorted((k, v["version"]) for k, v in rows.items())])
    indexed = (manager.learning_index.previous(cache_key, args["snapshot"]) if op in ("learning.tile", "learning.search") else None)
    indexed = indexed or manager.learning_index.get(cache_key, build)
    if indexed is None:
        guard()
        if op == "learning.snapshot":
            return {"snapshot": None, "index": manager.learning_index.status(), "ready": False}
        raise AssistantError("index_pending", "索引尚未就绪，请稍后读取快照")
    rows, version = indexed
    if op != "learning.snapshot" and args["snapshot"] != version:
        raise AssistantError("edit_conflict", "学习树已更新，请刷新快照")
    if op == "learning.snapshot":
        result = {"snapshot": version, "index": manager.learning_index.status(), "ready": True, "count": len(rows), "tile_size": 1024,
                  "bounds": [0, 0, max((r["x"] for r in rows.values()), default=0) + 256,
                             max((r["y"] for r in rows.values()), default=0) + 96]}
    elif op in ("learning.search", "learning.tile"):
        offset = args["offset"]
        if type(offset) is not int or offset < 0:
            invalid()
        if op == "learning.search":
            query, match = args["query"], args["filter"]
            if not isinstance(query, str) or len(query) > 256 or match not in ("all", "active", "archived", "due"):
                invalid()
            values = [r for r in rows.values() if query.casefold() in (r["title"] + " " + r["body"]).casefold()
                      and (match == "all" or match == "archived" and r["archived"] or match == "active" and not r["archived"]
                           or match == "due" and due(manager, goal, r)[1])]
        else:
            if any(type(args[k]) is not int or not 0 <= args[k] <= 1000000 for k in ("level", "x", "y")) or args["level"] > 12:
                invalid()
            size = 1024 << args["level"]
            values = [r for r in rows.values() if r["x"] // size == args["x"] and r["y"] // size == args["y"]]
        values.sort(key=lambda r: (r["x"], r["y"], r["id"]))
        result = {"snapshot": version, "total": len(values), "items": [strip(n) for n in values[offset:offset+50]],
                  "next_offset": offset + 50 if offset + 50 < len(values) else None}
    else:
        identifier = args["node"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identifier):
            invalid()
        row = rows.get(identifier)
        if op == "learning.detail":
            if not row:
                unavailable()
            current, is_due = due(manager, goal, row)
            data = row.get("record", {}).get("data", {})
            result = {**strip(row), "body": row["body"], "due": is_due,
                      "mastery": data.get("mastery") if current else None, "evidence_current": current,
                      "next_review_at": data.get("next_review_at") if current else None}
            if current:
                assessment = data["assessment"]
                if manager.admin.access.can_read(request, assessment["kind"] + "/" + assessment["id"]):
                    result["assessment"] = assessment
            result["source"] = {"kind": "learning_goals", "id": key, "version": goal["version"]}
            result["mistakes"] = [m for m in data.get("mistakes", []) if manager.admin.access.can_read(
                request, m["assessment"]["kind"] + "/" + m["assessment"]["id"])]
        else:
            current = rows
            changes = args["changes"]
            is_criterion = row is not None and row["type"] == "criterion"
            editable = {"parent_id", "prerequisites"} if is_criterion else {"title", "body", "parent_id", "prerequisites", "archived"}
            if not isinstance(changes, dict) or not changes or set(changes) - editable:
                invalid()
            if not row and not re.fullmatch(r"note-[a-f0-9]{32}", identifier):
                invalid()
            node = {**(row or {"id": identifier, "parent_id": None, "prerequisites": [], "archived": False, "body": ""}), **changes}
            if (not isinstance(node.get("title"), str) or not 1 <= len(node["title"].strip()) <= 500
                    or not isinstance(node["body"], str) or len(node["body"]) > 8000 or type(node["archived"]) is not bool
                    or node["parent_id"] is not None and not isinstance(node["parent_id"], str)
                    or not isinstance(node["prerequisites"], list) or len(node["prerequisites"]) > 20
                    or any(not isinstance(v, str) for v in node["prerequisites"])):
                invalid()
            links = node["prerequisites"] + ([node["parent_id"]] if node["parent_id"] else [])
            if is_criterion and any(current.get(k, {}).get("type") != "criterion" for k in links):
                raise AssistantError("learning_graph", "验收项只可依赖同一目标的验收项")
            graph([{"data": v} for v in {**current, identifier: node}.values()])
            with broker.lock, store.lock, store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                guard()
                goal = target()
                previous_ack = store.state(receipt_key, {}, db)
                if previous_ack.get("request") == fingerprint:
                    return previous_ack["result"]
                if goal["data"]["locked"] or store.state("desktop-last-restore", {}, db).get("status") == "assets_pending":
                    raise AssistantError("locked", "当前目标暂停编辑")
                live = criterion_versions(manager, request, goal, args["definition"])
                if (live != core_versions
                        or goal["version"] != cache_key[4] or stamp() != note_version
                        or (row["version"] if row else None) != args["expected"]):
                    raise AssistantError("edit_conflict", "节点已变更")
                if is_criterion:
                    data = {**row["record"]["data"], **changes}
                    saved = store.write("learning_nodes", data, row["version"], request.actor, identifier)["version"]
                else:
                    saved = uuid4().hex
                    store.set_state(prefix + ":note:" + identifier, {k: node[k] for k in editable | {"id"}} | {"version": saved}, db)
                    store.set_state(prefix + ":revision", saved, db)
                    changes_log = store.state(prefix + ":changes", [], db)
                    store.set_state(prefix + ":changes", [*changes_log[-127:],
                                    {"before": note_version, "after": saved, "id": identifier}], db)
                result = {"node": identifier, "version": saved, "refresh": "learning.snapshot"}
                store.set_state(receipt_key, {"request": fingerprint, "result": result}, db)
            # Commit the note and cursor first, then schedule this goal even if
            # the editor leaves the page before requesting a refreshed tile.
            try:
                operate(manager, request, "learning.snapshot", {k: args[k] for k in SCOPE})
            except AssistantError:
                pass  # Next authorized read resumes from the durable change log.
    guard()
    return result
