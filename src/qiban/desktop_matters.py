"""Safe desktop projections. Original messages and recipient IDs stay in encrypted storage."""

from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from companion_core import privacy
from companion_core.errors import AssistantError

LABEL = dict(
    credential="凭据（已遮盖）",
    unknown="待定片段",
    name="称呼片段",
    home="居住地点片段",
    venue="场馆地点片段",
    itinerary="行程片段",
    meeting="活动安排片段",
    ordinary="一般交流片段",
)
AUDIENCE = dict(
    credential="仅限本地受控使用",
    private="本人私聊或原资料范围",
    circle="已确认的相关参与者",
    social="适用的社交会话",
)


def tag(row):
    return sha256(row["id"].encode()).hexdigest()[:10].upper()


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def recipient_label(platform, kind, key):
    audience = "群聊" if kind == "group" else "私聊"
    return f"{platform.upper()} {audience} · {tag({'id': key})}"


def reminder_row(store, value):
    editable = value["status"] == "pending"
    with store.db() as db:
        record = db.execute(
            "SELECT conversation FROM core_events WHERE id=? AND deleted=0 AND platform=? AND account=?",
            (value.get("source_event"), value["platform"], value["account"]),
        ).fetchone()
    return {
        **{k: value[k] for k in ("id", "version", "status", "due_at")},
        "label": "提醒 · " + tag(value),
        "recipient_label": recipient_label(value["platform"], value["kind"], record[0]) if record else None,
        "can_reschedule": editable,
        "can_cancel": editable,
    }


def matter_row(store, db, row):
    from companion_core import role_matters

    label = f"{row['platform'].upper()} 自主事项 · {tag(row)}"
    body = role_matters.unpack(store, row)
    steps = body.get("steps") or []
    target = steps[0]["target"] if steps else body.get("recipient")
    recipient = None
    if target:
        record = db.execute(
            "SELECT c.platform,c.anchor,c.thread FROM matter_contacts c JOIN core_events e "
            "ON e.id=c.anchor AND e.deleted=0 WHERE c.id=?",
            (target,),
        ).fetchone()
        if record:
            from companion_core import events

            recipient = recipient_label(
                record[0], events.get(store, record[1])["event"]["visibility"], record[2]
            )
    wake = body.get("wake_at") or row["wake"] if row["status"] == "waiting" else None
    when = db.execute("SELECT updated_at FROM role_activity WHERE matter=?", (row["id"],)).fetchone()
    action = "chat" if steps else body.get("action")
    if action not in {"chat", "alarm", "end", "wait"}:
        action = "end" if row["status"] in {"completed", "stopped"} else "alarm" if wake else "wait"
    return {
        **{k: row[k] for k in ("id", "version", "status", "delivery")},
        "label": label,
        "action": action,
        "recipient_label": recipient,
        "wake_at": stamp(wake),
        "updated_at": stamp(when[0]) if when else None,
        "can_stop": row["status"] in {"ready", "waiting", "processing"},
    }


def recent(store):
    if getattr(store, "backend", None) != "postgres":
        return []
    with store.db() as db:
        rows = db.execute(
            "SELECT m.* FROM role_activity a JOIN role_matters m ON m.id=a.matter "
            "ORDER BY a.updated_at DESC,a.matter LIMIT 10"
        ).fetchall()
        return [matter_row(store, db, row) for row in rows]


def slice_row(store, row):
    kind, identifier = row["resource"].split("/", 1)
    item = {k: row[k] for k in ("id", "version", "status", "category")}
    item.update(kind=kind, identifier=identifier, label=f"{LABEL[row['category']]} · {tag(row)}")
    if row["status"] == "pending":
        return {**item, "level": None, "audience": "原资料范围", "choices": []}
    body = privacy.unpack(store, row)
    live = privacy.live_source(store, row, body)
    tier = privacy.level(store, row, body) if live else "private"
    if row["category"] == "credential":
        tier = "credential"
    choices = privacy.eligible_levels(store, row, body) if live and tier != "credential" else []
    audience = AUDIENCE[tier]
    if tier == "social" and row["category"] in privacy.SENSITIVE:
        audience = "适用的 QQ、微信社交会话，B 站公开评论除外"
    return {**item, "level": tier, "audience": audience, "choices": choices}


def operate(store, op, args):
    if any(v is not None and (not isinstance(v, str) or len(v) > 512) for v in args.values()):
        raise AssistantError("management_request", "请求标识或范围无效")
    key = args.get("id")
    with store.lock, store.db() as db:
        if op.endswith(".list"):
            where = ["id>?"]
            params = [args.get("after") or ""]
            if key:
                where.append("id=?")
                params.append(key)
            if op == "matters.list":
                source = "role_matters"
            else:
                for field, part in (("kind", 1), ("identifier", 2)):
                    if args.get(field):
                        where.append(f"split_part(resource,'/',{part})=?")
                        params.append(args[field])
                source = (
                    "(SELECT id,resource,version,status,level,category,fingerprint,body,source_version FROM privacy_slices "
                    "UNION ALL SELECT 'pending/'||resource,resource,source_version,'pending',NULL,'unknown',NULL,NULL,"
                    "source_version FROM privacy_jobs WHERE complete=0) q"
                )
            rows = db.execute(
                "SELECT * FROM " + source + " WHERE " + " AND ".join(where) + " ORDER BY id LIMIT 31", params
            ).fetchall()
            return {
                "items": [
                    matter_row(store, db, r) if op == "matters.list" else slice_row(store, r)
                    for r in rows[:30]
                ],
                "next": rows[29]["id"] if len(rows) > 30 else None,
            }
        db.execute("BEGIN IMMEDIATE")
        table = "role_matters" if op == "matters.stop" else "privacy_slices"
        row = db.execute("SELECT * FROM " + table + " WHERE id=? FOR UPDATE", (key,)).fetchone()
        if not row or row["version"] != args["expected"]:
            raise AssistantError("edit_conflict", "记录已经变化，请重新读取")
        if op == "matters.stop":
            if row["status"] not in {"ready", "processing", "waiting"}:
                raise AssistantError("edit_conflict", "事项状态已经改变")
            action = db.execute("SELECT status FROM core_actions WHERE id=?", (row["attempt"],)).fetchone()
            delivery = "uncertain" if action and action[0] == "dispatching" else row["delivery"]
            row = db.execute(
                "UPDATE role_matters SET status='stopped',delivery=?,version=? WHERE id=? RETURNING *",
                (delivery, uuid4().hex, key),
            ).fetchone()
            db.execute("DELETE FROM matter_watches WHERE matter=?", (key,))
            from companion_core.role_matters import touch

            touch(db, key)
            return matter_row(store, db, row)
        if args["level"] not in slice_row(store, row)["choices"]:
            raise AssistantError("privacy_scope", "当前证据不支持所选范围")
        db.execute(
            "INSERT INTO privacy_corrections VALUES(?,?,?) ON CONFLICT(resource,fingerprint) DO UPDATE SET level=excluded.level",
            (row["resource"], row["fingerprint"], args["level"]),
        )
        row = db.execute(
            "UPDATE privacy_slices SET level=?,status='corrected',version=? WHERE id=? RETURNING *",
            (args["level"], uuid4().hex, key),
        ).fetchone()
        return slice_row(store, row)
