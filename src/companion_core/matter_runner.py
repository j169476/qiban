"""Resume one tool decision or one recipient-specific delivery on each scheduled wake."""

from dataclasses import asdict
from uuid import uuid4

from . import privacy, role_actions
from . import role_matters as matters
from .contracts import ActionResult, stable_key
from .errors import AssistantError
from .governance import GovernedContext
from .schema import obj


async def decide(core, prompt, material, schema, guard):
    from . import context_workspace
    from .context_budget import pack

    pinned = context_workspace.revision(core.store)
    settings = context_workspace.policy(core.store.read("companion")["data"], pinned)
    prepared, _ = pack(material, settings, prompt, schema)

    def checked():
        guard()
        if context_workspace.revision(core.store)["version"] != pinned["version"]:
            raise AssistantError("context_changed", "模型资料方案已变化")

    return await core.model(prompt, prepared, schema, guard=checked, reserve_for_replies=1)


REPLY = obj({"send": {"type": "boolean"}, "text": {"type": "string", "maxLength": 300}})
WAKE_PROMPT = (
    "你被唤醒了。按人物卡和可见材料选择普通聊天的目标，也可以等待、设下一次闹钟、"
    "翻到下一页或结束。targets 仅填 contacts 中的 id，wake_at 使用带时区的未来时间。"
)
TEXT_PROMPT = (
    "为当前唯一的收件会话决定是否发一条普通消息，自行选择内容和语气。"
    "人物卡用于语气；称呼和个人事实须有本会话或 provided 中适用的来源。不得猜测私下谈话或身份。"
    "群内内容须适合全群。没有合适的话要说时返回 send=false。"
)


def materials(core, event, body):
    store = core.store
    access = GovernedContext(store, event)
    records = []
    pins = []
    crossed = []
    for ref in body["sources"]:
        try:
            src = matters.latest(store, ref)
        except AssistantError:
            continue
        key = "event/" + src.id
        if (
            src.thread == event.thread
            and access.access.can_read(access.request, key)
            and privacy.raw_allowed(store, access.request, key)
        ):
            access.resources.add(key)
            assertion = ref.get("assertion", body["cue"] if ref == body["sources"][0] else src.text)
            text = assertion if assertion in src.text else privacy.mask(src.text)
            records.append(
                {
                    "text": privacy.mask(text)[:500],
                    "said_at": src.occurred_at,
                    "speaker": ref["contact"],
                    "source": src.id,
                }
            )
        else:
            crossed.append(key)
    if crossed:
        values, pins = privacy.project(store, event, crossed)
        records.extend(values)
        access.fragment_pins.extend(pins)
    persona = access.persona_context()
    # A configured relationship describes the role. Its owner's identifying fields are not
    # evidence for addressing someone else. A reviewed name can still enter through records.
    card = persona.get("persona", {})
    if card.get("relationship_baseline"):
        from .person_baseline import matches

        matched = event.visibility == "private" and matches(store, event.actor)
        if not matched:
            persona["persona"] = {k: v for k, v in card.items() if k != "relationship_baseline"}
        access.guards.append(lambda: _check_relationship(store, event, matched))
    query = " ".join(rec["text"] for rec in records)[-200:]
    if (
        not query
        and access.access.can_read(access.request, "event/" + event.id)
        and privacy.raw_allowed(store, access.request, "event/" + event.id)
    ):
        access.resources.add("event/" + event.id)
        query = privacy.mask(event.text)[:160]
    if query:
        persona["memory"] = access.memory(query, only_current_member=event.visibility == "group")
    from . import conversations

    target = core.conversation_scope(event)
    tid = conversations.thread_id(target["account"], target["channel"], target["key"])
    signature = access.conversation_signature(target["account"], tid)
    persona["conversation"] = access.conversation(
        target["account"],
        tid,
        query=query,
        only_sender=target["sender_uid"] if event.visibility == "group" else None,
    )
    access.freeze()

    def check():
        access.check()
        privacy.check_projection(store, event, pins)
        if signature != access.conversation_signature(target["account"], tid):
            raise AssistantError("context_changed", "目标会话有新进展")

    check()
    return records, persona, check, access


def _check_relationship(store, event, expected):
    from .person_baseline import matches

    if (event.visibility == "private" and matches(store, event.actor)) != expected:
        raise AssistantError("context_changed", "收件会话的身份关联已改变")


async def tick(core, adapter, cores):
    store = core.store
    run = core.guard()
    if run["read_only"] or run.get("reply_test"):
        return
    claimed = matters.claim(core, adapter.platform, adapter.account)
    if not claimed:
        return
    identifier, attempt, version, body = claimed
    now = core.clock().timestamp()
    submitted = False

    def guard():
        core.guard(run["generation"])
        matters.latest(store, body["sources"][-1])
        _, _, _, basis = matters.resolve(cores, store, body["origin"], outbound=False)
        if basis != body["permission"]:
            raise AssistantError("matter_scope", "原会话许可已经改变")
        with store.db() as db:
            matters.owned(db, identifier, attempt, version)

    try:
        guard()
        if not body["steps"] or body["new_input"]:
            latest = matters.latest(store, body["sources"][-1])
            planner, _, _, _ = matters.resolve(cores, store, matters.contact_id(latest), outbound=False)
            records, persona, scope_check, planner_access = materials(planner, latest, body)
            contacts, cursor, choices, pins = role_actions.destinations(
                planner, latest, cores, body.get("after_contact", "")
            )
            note = body.get("alarm_note")
            own_note = ""
            if note and note.get("contact") == matters.contact_id(latest):
                own_note = matters.scoped_text(store, latest, note, planner_access)
            planner_access.fragment_pins.extend(pins)
            planner_access.freeze()

            def plan_guard():
                guard()
                scope_check()
                privacy.check_projection(store, latest, pins)

            targets_schema = (
                role_actions.target_args(contacts)["properties"]["targets"] | {"minItems": 0}
                if contacts
                else {"type": "array", "items": {"type": "string"}, "maxItems": 0}
            )
            plan = await decide(
                planner,
                WAKE_PROMPT,
                dict(
                    **persona,
                    provided=records,
                    contacts=choices,
                    more_contacts=bool(cursor),
                    origin=body["origin"],
                    own_note=own_note,
                    previous_result=body.get("last_result"),
                    now=core.clock().isoformat(),
                ),
                obj({"targets": targets_schema, "continuation": role_actions.CONTINUATION}),
                guard=plan_guard,
            )
            with store.lock, store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                plan_guard()
                row = matters.owned(db, identifier, attempt, version, for_update=True)
                role_actions.set_plan(
                    body,
                    plan["targets"],
                    contacts,
                    plan["continuation"],
                    cursor,
                    now,
                    timer=bool(body.get("wake_at")) and not body["new_input"],
                    note=role_actions.memo(store, latest, planner_access.proof(), plan["continuation"]),
                )
                body["failures"] = 0
                matters.advance(store, row, body, now, db)
            return
        step = body["steps"][0]
        sender, transport, event, basis = matters.resolve(cores, store, step["target"])
        if basis != step["basis"]:
            raise AssistantError("matter_scope", "收件会话许可已经改变")
        generation = sender.guard()["generation"]
        records, persona, scope_check, access = materials(sender, event, body)

        def send_guard():
            guard()
            sender.guard(generation)
            _, _, anchor, current = matters.resolve(cores, store, step["target"])
            if anchor.id != event.id or current != basis:
                raise AssistantError("context_changed", "收件会话已有新进展")
            scope_check()

        if event.visibility == "group":
            from .group_modes import initiative_slot
            from .reminders import scope_for

            with store.lock, store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                send_guard()
                row = matters.owned(db, identifier, attempt, version, for_update=True)
                available = initiative_slot(store, scope_for(store, event), sender.clock(), db, reserve=True)
                if available > now:
                    matters.update(store, row, body, "ready", available, db=db)
                    return
        result = await decide(
            sender,
            TEXT_PROMPT,
            dict(**persona, provided=records, audience=event.visibility, now=sender.clock().isoformat()),
            REPLY,
            guard=send_guard,
        )
        text = privacy.mask(result["text"]).strip()
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            send_guard()
            row = matters.owned(db, identifier, attempt, version, for_update=True)
            body["action"] = "chat"
            body["recipient"] = step["target"]
            if not result["send"] or not text or event.visibility == "group" and "@" in text:
                body["steps"].pop(0)
                body["last_result"] = "skipped"
                matters.advance(store, row, body, now, db)
                return
            journal = dict(
                event=event.as_dict(), arguments={"text": text}, matter=identifier, disclosure=access.proof()
            )
            db.execute(
                "INSERT INTO core_actions(id,dedupe,generation,platform,account,capability,body,status,created) "
                "VALUES(?,?,?,?,?,'message.reply',?,'dispatching',?)",
                (
                    attempt,
                    step["id"],
                    generation,
                    event.platform,
                    event.account,
                    store.ledger_encode("core_actions.body", journal),
                    sender.clock().isoformat(),
                ),
            )
            from .source_retention import action_refs

            action_refs(db, attempt, journal)
            db.execute(
                "INSERT INTO matter_watches VALUES(?,?) ON CONFLICT DO NOTHING", (identifier, step["target"])
            )
        submitted = True
        receipt = await transport.deliver_reminder(event, text, attempt, {"id": identifier}, guard=send_guard)
        if not isinstance(receipt, ActionResult):
            receipt = ActionResult("uncertain")
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE core_actions SET status=?,result=? WHERE id=?",
                (receipt.status, store.ledger_encode("core_actions.result", asdict(receipt)), attempt),
            )
            row = db.execute("SELECT * FROM role_matters WHERE id=? FOR UPDATE", (identifier,)).fetchone()
            delivery = {"succeeded": "sent", "cancelled": "cancelled", "failed": "cancelled"}.get(
                receipt.status, "uncertain"
            )
            if row["status"] == "processing" and row["attempt"] == attempt:
                body = matters.unpack(store, row)
                from . import delivery as deliveries

                deferred = receipt.status == "cancelled" and receipt.error in deliveries.TEMPORARY | {
                    "reply_quota_exhausted",
                    "qq_provider_unavailable",
                }
                retries = step.get("unsent", 0)
                if deferred and retries < deliveries.MAX_DEFERRALS and not body.get("new_input"):
                    reason = "quota_exhausted" if receipt.error == "reply_quota_exhausted" else receipt.error
                    due = max(
                        now + 300,
                        deliveries.retry_at(sender.clock(), store.read("policy")["data"], reason).timestamp(),
                    )
                    body["steps"][0].update(
                        unsent=retries + 1, id=stable_key("role-unsent", step["id"], retries + 1)
                    )
                    matters.update(store, row, body, "ready", due, db=db)
                    return
                body["steps"].pop(0)
                body["last_result"] = delivery
                if delivery == "sent":
                    body["recipient"] = step["target"]
                    body.setdefault("prompts", {})[step["target"]] = {
                        "text": text,
                        "pins": list(access.fragment_pins),
                        "disclosure": access.proof()["pins"],
                        "source": event.id,
                        "revision": matters.events.get(store, event.id)["version"],
                    }
                    matters.advance(store, row, body, now, db, delivery)
                else:
                    matters.update(
                        store,
                        row,
                        body,
                        "uncertain" if delivery == "uncertain" else "stopped",
                        0,
                        delivery=delivery,
                        db=db,
                    )
            elif row["status"] == "stopped":
                db.execute(
                    "UPDATE role_matters SET delivery=?,version=? WHERE id=?",
                    (delivery, uuid4().hex, identifier),
                )
                matters.touch(db, identifier)
        if receipt.status == "succeeded":
            sender._record_reply(
                event,
                {"capability": "message.reply", "arguments": {"text": text}},
                {"id": attempt, "status": "succeeded"},
                sender.clock().isoformat(),
                access.proof(),
            )
    except AssistantError as exc:
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM role_matters WHERE id=? AND status='processing' AND attempt=?",
                (identifier, attempt),
            ).fetchone()
            if not row:
                return
            body = matters.unpack(store, row)
            if submitted:
                db.execute(
                    "UPDATE core_actions SET status='uncertain' WHERE id=? AND status='dispatching'",
                    (attempt,),
                )
                matters.update(store, row, body, "uncertain", 0, delivery="uncertain", db=db)
            elif body.get("pending_plan"):
                matters.advance(store, row, body, now, db)
            elif exc.code == "role_alarm_time":
                body.update(steps=[], wake_at=None, next="wait", action="wait", new_input=False)
                matters.advance(store, row, body, now, db)
            elif exc.code in {"matter_scope", "reply_not_authorized", "access_denied", "unknown_evidence"}:
                matters.update(store, row, body, "stopped", 0, db=db)
            else:
                if exc.code == "model_quota":
                    from .delivery import retry_at

                    wake = retry_at(core.clock(), store.read("policy")["data"], exc.code).timestamp()
                elif body["new_input"]:
                    wake = now
                else:
                    retries = body.get("failures", 0) + 1
                    body["failures"] = retries
                    if retries >= 3:
                        body.update(steps=[], next="wait", wake_at=None, action="wait")
                        matters.advance(store, row, body, now, db)
                        return
                    wake = now + 300 * retries
                matters.update(store, row, body, "ready", wake, db=db)
