"""Claim one due reminder, retrieve under its audience, journal before external submission."""
import asyncio
from dataclasses import asdict
from uuid import uuid4

from . import conversations, delivery, events, reminders
from .contracts import ActionResult, Event, stable_key
from .errors import AssistantError
from .governance import GovernedContext
from .schema import obj

LEASE = 300


def verify_bili_attempt(store, db, run, payload, attempt, target_uid):
    row = db.execute("SELECT a.body,r.version,r.id FROM core_actions a JOIN role_reminders r ON r.attempt=a.id "
                     "WHERE a.id=? AND a.platform='bilibili' AND a.account=? AND a.generation=? "
                     "AND a.status='dispatching' AND r.status='processing'",
                     (attempt, run["account"], run["generation"])).fetchone()
    if row:
        body = store.ledger_decode("core_actions.body", row["body"])
        event = Event.from_dict(body["event"])
        if (body["reminder"] == row["id"] and body["reminder_version"] == row["version"]
                and body["arguments"] == {"text": payload["text"]} and event.visibility == "private"
                and event.native_id == payload["event_id"] and event.actor.user_id == target_uid):
            return
    action = db.execute("SELECT a.body,m.id FROM core_actions a JOIN role_matters m ON m.attempt=a.id "
                        "WHERE a.id=? AND a.platform='bilibili' AND a.account=? AND a.generation=? "
                        "AND a.status='dispatching' AND m.status='processing'",
                        (attempt,run["account"],run["generation"])).fetchone()
    if action:
        body = store.ledger_decode("core_actions.body",action["body"])
        event = Event.from_dict(body["event"])
        if (body.get("matter")==action["id"] and body["arguments"]=={"text":payload["text"]}
                and event.visibility=="private" and event.native_id==payload["event_id"] and event.actor.user_id==target_uid):
            return
    raise AssistantError("reminder_scope", "提醒缺少原会话及当前领取记录")

# Existing alarms retain their original recipient and ledger. New alarms live in role_matters.
LEGACY_PROMPT = ("这个已保存的定时记录唤醒了你。可在原授权会话使用普通聊天、重新设钟或结束。"
                 "只使用当前材料，按人物卡自行决定内容与时机。")


def claim(store, adapter, now):
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM role_reminders WHERE platform=? AND account=? AND due<=? "
                         "AND status IN ('pending','processing') AND wake_after<=? ORDER BY due,id LIMIT 1 FOR UPDATE SKIP LOCKED",
                         (adapter.platform, adapter.account, now, now)).fetchone()
        if not row:
            return None
        if row["attempt"]:
            action = db.execute("SELECT status FROM core_actions WHERE id=?", (row["attempt"],)).fetchone()
            if action:
                status = {"succeeded": "sent", "failed": "cancelled", "cancelled": "cancelled"}.get(action[0], "uncertain")
                finish(store, {"id": row["id"]}, row["attempt"], status)
                if action[0] == "dispatching":
                    db.execute("UPDATE core_actions SET status='uncertain' WHERE id=? AND status='dispatching'", (row["attempt"],))
                return None
        token = uuid4().hex
        db.execute("UPDATE role_reminders SET status='processing', attempt=?,wake_after=? WHERE id=?",
                   (token, now + LEASE, row["id"]))
        return reminders.decode(store, row), token


def finish(store, job, attempt, status, wake_after=0):
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("UPDATE role_reminders SET status=?,version=?,wake_after=?,attempt=NULL "
                   "WHERE id=? AND status='processing' AND attempt=?",
                   (status, uuid4().hex, wake_after, job["id"], attempt))
        if changed.rowcount and status != "pending":
            db.execute("DELETE FROM state WHERE key=?", ("alarm-source:" + job["id"],))


def require_source(core, event, job, scope, generation):
    run = core.guard(generation)
    if run["read_only"] or run.get("reply_test") or [event.platform, event.account] not in run["accounts"]:
        raise AssistantError("reminder_scope", "当前运行不能发送提醒")
    if events.versions(core.store, [event.id])[event.id] != job["source_version"]:
        raise AssistantError("reminder_scope", "原消息已改变")
    if scope and reminders.permission_basis(core.store, scope) != job["permission"]:
        raise AssistantError("reply_not_authorized", "原会话权限或成员关系已变化")
    if job["source"] == "care":
        if not job.get("care_permission") or reminders.care_permission(core.store, event) != job["care_permission"]:
            raise AssistantError("reminder_scope", "原主动关心条件已改变")


async def tick(core, adapter):
    run = core.guard()
    store = core.store
    if (getattr(store, "backend", None) != "postgres" or not hasattr(adapter, "deliver_reminder")
            or run["read_only"] or run.get("reply_test")):
        return
    now = core.clock().timestamp()
    claimed = await asyncio.to_thread(claim, store, adapter, now)
    if not claimed:
        return
    job, attempt = claimed
    submitted = False
    try:
        event = Event.from_dict(events.get(store, job["source_event"])["event"])
        scope = reminders.scope_for(store, event)
        if hasattr(adapter, "reminder_eligible"):
            adapter.reminder_eligible(event)
        require_source(core, event, job, scope, run["generation"])
        if not core.remaining_model_calls():
            wake = delivery.retry_at(core.clock(), store.read("policy")["data"], "model_quota")
            finish(store, job, attempt, "pending", wake.timestamp())
            return
        if job["source"] == "care" and scope and scope.kind == "group":
            from .group_modes import initiative_slot
            with store.lock, store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                instant = core.clock()
                next_slot = initiative_slot(store, scope, instant, db, reserve=True)
                if next_slot > instant.timestamp():
                    finish(store, job, attempt, "pending", next_slot)
                    return
        access = GovernedContext(store, event)
        access.resources.add("event/" + event.id)
        from .privacy import raw_allowed
        if not raw_allowed(store,access.request,"event/"+event.id):
            finish(store,job,attempt,"cancelled")
            return
        material = access.memory(job["clue"], only_current_member=event.visibility == "group")
        target = core.conversation_scope(event)
        tid = conversations.thread_id(target["account"], target["channel"], target["key"])
        prior = access.conversation_signature(target["account"], tid)
        history = access.conversation(target["account"], tid, query=job["clue"],
                                      only_sender=target["sender_uid"] if event.visibility == "group" else None)
        context = {**access.persona_context(), "clue": job["clue"], "due_at": job["due_at"],
                   "now": core.clock().isoformat(), "conversation": history, "memory": material}
        from .context_budget import pack
        from .role_actions import CONTINUATION
        arguments = obj({"text":{"type":"string","maxLength":300}})
        if event.visibility == "group":
            context["recipient"] = event.actor.key
            arguments = obj({**arguments["properties"], "recipient":{"type":"string","maxLength":128}})
        control = obj({**CONTINUATION["properties"], "next":{"enum":["end","stop","alarm"]}})
        schema = obj({"action":{"anyOf":[{"type":"null"},obj({"capability":{"const":"message.reply"},
                                                              "arguments":arguments})]},
                      "continuation":{"anyOf":[{"type":"null"},control]}})
        context, _ = pack(context, store.read("companion")["data"], LEGACY_PROMPT, schema)
        access.freeze()

        def guard():
            if hasattr(adapter, "reminder_eligible"):
                adapter.reminder_eligible(event)
            require_source(core, event, job, scope, run["generation"])
            access.check()
            if not raw_allowed(store,access.request,"event/"+event.id):
                raise AssistantError("reminder_scope","提醒来源的适用范围已改变")
            if access.conversation_signature(target["account"], tid) != prior:
                raise AssistantError("context_changed", "到期处理期间会话有新进展")
            with store.db() as db:
                current = db.execute("SELECT version,status,attempt FROM role_reminders WHERE id=?", (job["id"],)).fetchone()
            if not current or tuple(current) != (job["version"], "processing", attempt):
                raise AssistantError("reminder_changed", "提醒已改变")
        result = await core.model(LEGACY_PROMPT, context, schema, guard=guard)
        guard()
        action, control = result["action"], result["continuation"]
        if action is None:
            from .role_actions import alarm_time
            deferred = control and control["next"] == "alarm"
            key = stable_key("alarm-defer", job["id"], job["due_at"])
            with store.lock, store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                guard()
                wake = alarm_time(control, now) if deferred and not store.state(key, False, db) else 0
                if wake:
                    store.set_state(key, True, db)
                final = "cancelled" if control and control["next"] == "stop" else "completed"
                finish(store, job, attempt, "pending" if wake else final, wake)
            return
        if event.visibility == "group" and action["arguments"]["recipient"] != event.actor.key:
            finish(store, job, attempt, "cancelled")
            return
        from .privacy import mask
        text = mask(action["arguments"]["text"]).strip()
        if not text:
            finish(store, job, attempt, "completed")
            return
        if event.visibility == "group":
            if "@" in text:
                finish(store, job, attempt, "completed")
                return
            text = f"给 {event.actor.user_id}：{text}"
        dedupe = stable_key("alarm-send", job["id"], job["version"])
        body = {"event": event.as_dict(), "arguments": {"text": text}, "reminder": job["id"],
                "reminder_version": job["version"], "disclosure": access.proof()}
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            guard()
            db.execute("INSERT INTO core_actions(id,dedupe,generation,platform,account,capability,body,status,created) "
                       "VALUES(?,?,?,?,?,'message.reply',?,'dispatching',?)",
                       (attempt, dedupe, run["generation"], event.platform, event.account,
                        store.ledger_encode("core_actions.body", body), core.clock().isoformat()))
            from .source_retention import action_refs
            action_refs(db, attempt, body)
        submitted = True
        result = await adapter.deliver_reminder(event, text, attempt, job, guard=guard)
        if not isinstance(result, ActionResult):
            result = ActionResult("uncertain")
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE core_actions SET status=?,result=? WHERE id=?",
                       (result.status, store.ledger_encode("core_actions.result", asdict(result)), attempt))
            final = {"succeeded": "sent", "cancelled": "cancelled", "failed": "cancelled"}.get(result.status, "uncertain")
            wake = 0
            if result.status == "cancelled" and result.error in delivery.TEMPORARY | {"reply_quota_exhausted", "qq_provider_unavailable"}:
                # A known pre-submit rejection can be reconsidered with fresh memory and permissions.
                retry_key = stable_key("alarm-unsent", job["id"], job["due_at"])
                retries = store.state(retry_key, 0, db)
                if retries < delivery.MAX_DEFERRALS:
                    reason = "quota_exhausted" if result.error == "reply_quota_exhausted" and event.visibility == "private" else result.error
                    wake = max(now + 300, delivery.retry_at(core.clock(), store.read("policy")["data"], reason).timestamp())
                    if result.error == "reply_quota_exhausted" and scope and scope.kind == "group":
                        minutes = adapter.participation.policies.effective(scope, db)["settings"]["group_session_minutes"]
                        wake = max(wake, now + minutes * 60)
                    store.set_state(retry_key, retries + 1, db)
                    final = "pending"
            finish(store, job, attempt, final, wake)
        if result.status == "succeeded":
            core._record_reply(event, {"capability": "message.reply", "arguments": {"text": text}},
                               {"id": attempt, "status": "succeeded"}, core.clock().isoformat(), access.proof())
    except AssistantError as exc:
        if submitted:
            with store.lock, store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE core_actions SET status='uncertain' WHERE id=? AND status='dispatching'", (attempt,))
                finish(store, job, attempt, "uncertain")
        elif exc.code in {"reply_not_authorized", "reminder_scope", "access_denied", "unknown_evidence"}:
            finish(store, job, attempt, "cancelled")
        else:
            finish(store, job, attempt, "pending", now + 300)
        return
