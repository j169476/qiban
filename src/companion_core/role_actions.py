"""Ordinary chat destinations and durable continuation, shared by inbox and timer turns."""

from dataclasses import replace
from datetime import datetime

from . import privacy, reminders
from . import role_matters as matters
from .contracts import stable_key
from .errors import AssistantError
from .schema import obj

CONTINUATION = obj(
    {
        "matter": {"type": ["string", "null"]},
        "next": {"enum": ["wait", "alarm", "end", "stop", "more_contacts"]},
        "wake_at": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"], "maxLength": 160},
    },
    required=["matter", "next", "wake_at"],
)
GUIDANCE = (
    "message.reply 是普通聊天。给当前会话可直接写正文，选其他可见会话时只填写 targets，"
    "系统会按每个收件会话可见的材料分别撰写正文。contacts 给出本轮可用的会话。"
    "continuation 可选择等新消息、定时叫醒自己、看下一页会话或结束。"
    "处于 waiting 中的已有事项可以用它的 id 继续；无关的事情单独处理。"
    "alarm 的 wake_at 是带时区的未来时间。响铃后自行决定做什么。end 是完成，stop 是取消。"
)


def destinations(core, event, cores, after=""):
    from .person_baseline import matches

    contacts, cursor = matters.candidates(cores, core.store, after)
    choices, pins = [], []
    for key, (target, _) in contacts.items():
        with core.store.db() as db:
            roots = [
                "event/" + r[0]
                for r in db.execute(
                    "SELECT id FROM core_events WHERE deleted=0 AND actor=? AND conversation=? "
                    "ORDER BY occurred DESC,id LIMIT 12",
                    (target.actor.key, target.thread),
                )
            ]
        known, dependencies = privacy.project(core.store, event, roots, limit=2)
        for fragment in known:
            fragment["text"] = fragment["text"][:120]
        pins.extend(dependencies)
        choice = {
            "id": key,
            "platform": target.platform,
            "audience": target.visibility,
            "current": key == matters.contact_id(event),
            "known_context": known,
        }
        if target.visibility == "group":
            choice["conversation"] = destination_key(target)
        if (
            event.visibility == "private"
            and target.visibility == "private"
            and matches(core.store, target.actor)
        ):
            choice["configured_relationship"] = True
        choices.append(choice)
    return contacts, cursor, choices, pins


def destination_key(event):
    if event.platform == "qq":
        return stable_key(
            "chat-destination",
            event.platform,
            event.account,
            event.visibility,
            event.metadata["qq"]["target_id"],
        )
    return event.thread


def target_args(contacts):
    ids = list(contacts)
    return obj(
        {
            "targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(ids),
                "items": {"enum": ids},
                "uniqueItems": True,
            }
        }
    )


def capabilities(offered, contacts):
    if not contacts:
        return offered
    return [
        replace(
            cap,
            description=cap.description + "。也可选择可见会话 targets，由该收件范围生成正文。",
            parameters={"anyOf": [cap.parameters, target_args(contacts)]},
        )
        if cap.name == "message.reply"
        else cap
        for cap in offered
    ]


def alarm_time(control, now):
    try:
        stamp = datetime.fromisoformat(control["wake_at"])
        if stamp.utcoffset() is None or stamp.timestamp() <= now:
            raise ValueError
    except (TypeError, ValueError, OverflowError, OSError):
        raise AssistantError("role_alarm_time", "唤醒时间须为带时区的未来时间") from None
    return stamp.timestamp()


def memo(store, event, proof, control):
    if not control or control["next"] != "alarm" or not control.get("note"):
        return None
    return {
        "contact": matters.contact_id(event),
        "text": privacy.mask(control["note"]),
        "source": event.id,
        "revision": matters.events.get(store, event.id)["version"],
        "disclosure": proof["pins"],
        "pins": proof["privacy"],
    }


def set_plan(body, targets, contacts, control, cursor, now, *, timer=False, note=None):
    body["round"] += 1
    body["new_input"] = False
    seen = set()
    body["steps"] = []
    for target in targets:
        if target not in contacts:
            continue
        event, basis = contacts[target]
        key = destination_key(event)
        if key in seen:
            continue
        seen.add(key)
        body["steps"].append(
            {
                "target": target,
                "basis": basis,
                "id": stable_key(
                    "role-chat",
                    body["origin"],
                    body["sources"][0]["event"],
                    body["round"],
                    len(body["steps"]),
                ),
            }
        )
    choice = control["next"] if control else "end"
    body.update(
        next="finish" if choice == "end" else "stop" if choice == "stop" else "wait",
        wake_at=None,
        after_contact="",
        alarm_note=None,
    )
    if choice == "more_contacts" and cursor:
        body.update(next="continue", after_contact=cursor)
    if choice == "alarm":
        due = alarm_time(control, now)
        # A newly received assertion or a first confirmed contact is real progress.
        # Our own alarm wording and repeated sends to the same peer are not.
        progress = stable_key(
            "wake-progress",
            [(r["event"], r["version"]) for r in body["sources"]],
            sorted(body.get("prompts", {})),
        )
        unchanged = timer and progress == body.get("wake_progress")
        repeats = body.get("idle_wakes", 0) + 1 if unchanged else 0
        body["wake_progress"] = progress
        body["idle_wakes"] = repeats
        if repeats <= 4:
            floor = (0, 30, 120, 600, 3600)[repeats]
            body["wake_at"] = max(due, now + max(1, floor))
            body["alarm_note"] = note
    if body["steps"]:
        body["recipient"] = body["steps"][0]["target"]
    body["action"] = (
        "chat"
        if body["steps"]
        else ("alarm" if body["wake_at"] else "end" if body["next"] in {"finish", "stop"} else "wait")
    )


def commit(core, event, version, targets, control, contacts, cursor, waiting, db, sources, proof):
    store = core.store
    previous = control and control.get("matter")
    if not targets and not previous and (not control or control["next"] in {"end", "stop"}):
        return
    receipt = stable_key("role-turn", event.id, version)
    if store.state(receipt, None, db) is not None:
        return
    # The last pre-upgrade inbox may have committed its local effect before acknowledging intake.
    if any(store.state(stable_key("role-tool-result", s.id, v), None, db) for s, v in sources):
        store.set_state(receipt, "previously_applied", db)
        return
    now = core.clock().timestamp()
    if control and control["next"] == "alarm":
        alarm_time(control, now)
    if previous:
        snapshot = next((item for item in waiting if item["id"] == previous), None)
        if snapshot and snapshot.get("bound_timer"):
            operation = {"alarm": "reschedule", "end": "complete", "stop": "cancel"}.get(control["next"])
            if targets or not operation:
                raise AssistantError("matter_scope", "旧定时记录限于原会话的改期或结束")
            reminders.change(
                store,
                previous,
                snapshot["version"],
                operation,
                now,
                due_at=control.get("wake_at"),
                scope=reminders.owner(event),
            )
            store.set_state(receipt, previous, db)
            return
        row = db.execute(
            "SELECT m.* FROM role_matters m JOIN matter_watches w ON w.matter=m.id "
            "WHERE m.id=? AND w.contact=? FOR UPDATE OF m",
            (previous, matters.contact_id(event)),
        ).fetchone()
        if (
            not snapshot
            or not row
            or row["version"] != snapshot["version"]
            or row["status"] not in {"ready", "waiting", "processing"}
        ):
            raise AssistantError("edit_conflict", "等待中的事项已改变")
    else:
        source, revision = sources[0]
        identifier = matters.create(store, source, revision, now, db)
        row = db.execute("SELECT * FROM role_matters WHERE id=? FOR UPDATE", (identifier,)).fetchone()
        if row["status"] in {"stopped", "completed", "uncertain"}:
            return
    body = matters.unpack(store, row)
    for source, revision in sources:
        if not any(ref["event"] == source.id and ref["version"] == revision for ref in body["sources"]):
            body["sources"].append(matters.evidence(source, revision, source.text[:200]))
    body["idle_wakes"] = 0
    body["failures"] = 0
    note = memo(store, event, proof, control)
    # A reply can arrive during an in-flight send. Preserve its lease and ledger until its receipt is known.
    if row["status"] == "processing":
        from copy import deepcopy
        from uuid import uuid4

        pending = deepcopy(body)
        set_plan(pending, targets, contacts, control, cursor, now, note=note)
        body["pending_plan"] = {
            key: pending.get(key)
            for key in (
                "steps",
                "round",
                "next",
                "wake_at",
                "after_contact",
                "action",
                "idle_wakes",
                "wake_progress",
                "alarm_note",
            )
        }
        body["new_input"] = True
        db.execute(
            "UPDATE role_matters SET body=?,version=? WHERE id=?",
            (matters.pack(store, row["id"], body), uuid4().hex, row["id"]),
        )
    else:
        set_plan(body, targets, contacts, control, cursor, now, note=note)
        matters.advance(store, row, body, now, db)
    matters.retain(store, db, row["id"], body)
    store.set_state(receipt, row["id"], db)
