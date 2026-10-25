"""Durable receipts and recoverable YAML projection, preserving manual edits."""

import hashlib
import json

import yaml

from .errors import AssistantError
from .storage import atomic_write, digest, utcnow


def receipt(uid, event_id):
    return "relationship:" + hashlib.sha256((uid + ":" + event_id).encode()).hexdigest()


def pending(store, uid=None):
    with store.db() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM relationship_pending" + (" WHERE uid=?" if uid else ""),
            (uid,) if uid else (),
        )]


def current_version(store, uid):
    return store.version("relationships", uid)


def finish(store, item):
    with store.db() as db:
        # Receipt remains durable after the recoverable projection is removed.
        store.set_state(item["receipt"], True, db)
        db.execute("DELETE FROM relationship_pending WHERE receipt=?", (item["receipt"],))


def conflict(store, item, reason):
    path = store.root / "conflicts" / ("relationship-pending-" + item["receipt"].split(":")[1] + ".yaml")
    if getattr(store, "backend", "yaml") != "postgres" and not path.exists():
        atomic_write(path, item["after_yaml"].encode("utf-8"))
    with store.db() as db:
        db.execute("UPDATE relationship_pending SET status='conflict',error=? WHERE receipt=?",
                   (reason, item["receipt"]))


def recover(store, uid=None):
    # Opening a store must not wait for an unrelated in-flight platform action's lock.
    if not pending(store, uid):
        return
    with store.lock:
        for item in pending(store, uid):
            try:
                actual = current_version(store, item["uid"])
                if actual == item["after_version"]:
                    finish(store, item)
                elif actual == item["before_version"]:
                    store.write("relationships", json.loads(item["data"]), actual,
                                item["actor"], item["uid"])
                    finish(store, item)
                else:
                    conflict(store, item, "manual_edit")
            except (AssistantError, OSError) as exc:
                conflict(store, item, exc.code if isinstance(exc, AssistantError) else "file_io")


def status(store):
    result = []
    for item in pending(store):
        try:
            version = current_version(store, item["uid"])
        except OSError:
            version = "unreadable"
        result.append({"uid": item["uid"], "event_id": item["event_id"], "status": item["status"],
                       "error": item["error"], "current_version": version,
                       "before_version": item["before_version"], "pending_version": item["after_version"]})
    return result


def commit(store, doc, expected, event_id, actor):
    store.profile.validate("relationships", doc)
    raw = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8")
    key = receipt(doc["id"], event_id)
    if getattr(store, "backend", "yaml") == "postgres":
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if store.state(key, False, db):
                return store.read("relationships", doc["id"])
            if pending(store, doc["id"]):
                raise AssistantError("relationship_recovery_conflict", "关系资料有旧版待裁决提交")
            result = store.write("relationships", doc, expected, actor, doc["id"])
            store.set_state(key, True, db)
            return result
    with store.lock:
        if pending(store, doc["id"]):
            raise AssistantError("relationship_recovery_conflict", "该 UID 有待裁决的关系提交，请在管理入口检查")
        if current_version(store, doc["id"]) != expected:
            raise AssistantError("edit_conflict", "关系文件已人工修改，请重新读取")
        if doc.get("locked"):
            raise AssistantError("locked", "该条目已锁定，保留本地版本")
        with store.db() as db:
            db.execute("INSERT INTO relationship_pending VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                key, doc["id"], event_id, expected, digest(raw), json.dumps(doc, ensure_ascii=False),
                raw.decode("utf-8"), actor, utcnow(), "prepared", "",
            ))
            store.set_state(key, True, db)
        # A failure from here onward leaves both the receipt and recoverable payload.
        result = store.write("relationships", doc, expected, actor, doc["id"])
        finish(store, {"receipt": key})
        return result


def resolve(store, uid, choice, expected):
    if choice not in ("keep_local", "apply_pending"):
        raise AssistantError("invalid_resolution", "选择保留人工版本或应用待提交版本")
    with store.lock:
        items = pending(store, uid)
        if not items:
            raise AssistantError("not_found", "该 UID 没有待裁决提交")
        item = items[0]
        if current_version(store, uid) != expected:
            raise AssistantError("edit_conflict", "关系文件已变化，请重新检查")
        if choice == "apply_pending":
            store.write("relationships", json.loads(item["data"]), expected, "admin-recovery", uid,
                        allow_locked=True)
        elif expected is not None:
            store.read("relationships", uid)  # Do not silently bless malformed local YAML.
        finish(store, item)
        store.audit("recovery", {"uid": uid, "choice": choice, "event_id": item["event_id"]})
        return {"resolved": True, "uid": uid, "choice": choice, "event_consumed": True}
