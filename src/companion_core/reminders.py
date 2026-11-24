"""Durable reminder records. A reminder stays bound to its original actor and audience."""
from datetime import datetime, timezone
from uuid import uuid4

from .contracts import stable_key
from .errors import AssistantError
from .reply_authorization import ReplyScope

PAGE = 30


def time_value(value, now):
    try:
        when = datetime.fromisoformat(value)
        if when.utcoffset() is None or when.timestamp() <= now:
            raise ValueError()
        return when.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        raise AssistantError("reminder_time", "请选择带时区的未来时间") from None


def scope_for(store, event):
    if event.platform == "qq":
        return ReplyScope("qq", event.account, event.visibility, event.metadata["qq"]["target_id"])
    if event.platform == "wechat":
        registry = store.state(stable_key("wechat-bindings-v1", event.account), {})
        binding = registry.get("conversations", {}).get(event.conversation, {})
        if not binding.get("enabled") or binding.get("target_ref") != event.metadata["wechat"]["target_ref"]:
            raise AssistantError("reminder_scope", "微信会话绑定已经失效")
        return ReplyScope("wechat", registry["identity"], event.visibility, binding["target_ref"])
    return None


def owner(event):
    return stable_key("reminder-owner", event.thread, event.actor.key)


def care_permission(store, event):
    from .group_modes import observing, settings
    from .role_affinity import close_contact
    if not close_contact(store, event):
        return None
    if event.visibility == "private":
        return "private"
    if event.visibility == "group" and event.platform == "qq":
        scope = scope_for(store, event)
        mode = settings(store, scope)
        if (mode["proactive"] and permission_basis(store, scope)
                and observing(store, scope, datetime.fromisoformat(event.occurred_at).timestamp())):
            return mode["version"]
    return None


def permission_basis(store, scope, db=None):
    from .reply_audience import Audience, read, registry
    from .reply_authorization import ReplyAuthorizations
    from .reply_exclusion import ReplyExclusions
    permit = ReplyAuthorizations(store).version(scope, db=db)
    if not permit:
        return None
    category = Audience(scope.platform, scope.account_identity, scope.kind)
    policy = read(store, category, db)
    if not policy["enabled"]:
        return permit
    live = registry(store).get(category)
    identity = live.identity(policy["version"], scope.target_id) if live else None
    if not identity:
        return None
    return stable_key("reminder-member", scope.id, policy["version"], identity,
                      ReplyExclusions(store).record(scope, db=db)["version"])


def decode(store, row):
    body = store.ledger_decode("role-reminder:" + row["id"], row["body"])
    return {**body, "id": row["id"], "version": row["version"], "status": row["status"],
            "platform": row["platform"], "account": row["account"],
            "due_at": datetime.fromtimestamp(row["due"], timezone.utc).isoformat()}


def public(value):
    return {k: value[k] for k in ("id", "version", "status", "platform", "account", "target", "kind",
                                  "clue", "source", "due_at")}


def listing(store, after=None, *, status=None, id=None, project=public):
    if status not in (None, "pending") or after is not None and not isinstance(after, str):
        raise AssistantError("management_request", "提醒分页参数无效")
    with store.db() as db:
        if id is not None:
            if not isinstance(id, str) or len(id) > 160:
                raise AssistantError("management_request", "提醒标识无效")
            row = db.execute("SELECT * FROM role_reminders WHERE id=?", (id,)).fetchone()
            return {"items": [project(decode(store, row))] if row and (not status or status == row["status"]) else [],
                    "next": None}
        if status:
            boundary = db.execute("SELECT due,id FROM role_reminders WHERE id=?", (after,)).fetchone() if after else None
            if after and not boundary:
                raise AssistantError("edit_conflict", "分页位置已改变")
            where = " AND (due,id)>(?,?)" if boundary else ""
            rows = db.execute("SELECT * FROM role_reminders WHERE status='pending'" + where +
                              " ORDER BY due,id LIMIT ?", (*tuple(boundary or ()), PAGE + 1)).fetchall()
        else:
            where = " WHERE id<?" if after else ""
            rows = db.execute("SELECT * FROM role_reminders" + where + " ORDER BY id DESC LIMIT ?",
                              (after, PAGE + 1) if after else (PAGE + 1,)).fetchall()
        return {"items": [project(decode(store, r)) for r in rows[:PAGE]],
                "next": rows[PAGE - 1]["id"] if len(rows) > PAGE else None}


def pending(store, event):
    with store.db() as db:
        rows = db.execute("SELECT * FROM role_reminders WHERE scope=? AND status='pending' ORDER BY due,id LIMIT 8",
                          (owner(event),)).fetchall()
        return [public(decode(store, r)) for r in rows]


def change(store, identifier, expected, operation, now, *, due_at=None, scope=None, project=public):
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM role_reminders WHERE id=? FOR UPDATE", (identifier,)).fetchone()
        if row is None or scope is not None and scope != row["scope"]:
            raise AssistantError("reminder_unknown", "原会话中没有这条提醒")
        if row["version"] != expected:
            raise AssistantError("edit_conflict", "提醒已经变化，请重新读取")
        if row["status"] != "pending":
            raise AssistantError("reminder_state", "提醒已进入投递或已结束")
        due = time_value(due_at, now) if operation == "reschedule" else row["due"]
        state = {"cancel": "cancelled", "complete": "completed", "reschedule": "pending"}[operation]
        db.execute("UPDATE role_reminders SET due=?,status=?,version=?,wake_after=0 WHERE id=?",
                   (due, state, uuid4().hex, identifier))
        if state != "pending":
            db.execute("DELETE FROM state WHERE key=?", ("alarm-source:" + identifier,))
        return project(decode(store, db.execute("SELECT * FROM role_reminders WHERE id=?", (identifier,)).fetchone()))
