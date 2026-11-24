"""Durable role matters. Sources are separate assertions, anchored to their authors."""

from uuid import uuid4

from . import events, reminders
from .contracts import Event, stable_key
from .errors import AssistantError


def contact_id(event):
    return stable_key("contact", event.thread, event.actor.key)


def authority(store, event, adapter, *, outbound):
    if (
        not outbound
        and event.platform == "bilibili"
        and event.visibility == "public"
        and hasattr(adapter, "public_origin")
        and adapter.public_origin(event)
    ):
        return stable_key("public-origin", event.thread, event.actor.key)
    if event.visibility not in {"private", "group"}:
        return None
    if outbound and (event.platform not in {"qq", "bilibili"} or not hasattr(adapter, "deliver_reminder")):
        return None
    if (
        event.platform == "local"
        and not outbound
        and event.visibility == "private"
        and hasattr(adapter, "accepted_for_run")
    ):
        run = adapter.core.guard()
        return (
            stable_key("room-matter", event.thread, event.actor.key)
            if adapter.accepted_for_run(event, run)
            else None
        )
    if event.platform in {"qq", "wechat"}:
        target_field = "target_id" if event.platform == "qq" else "target_ref"
        if not event.metadata.get(event.platform, {}).get(target_field):
            return None
        scope = reminders.scope_for(store, event)
        permission = reminders.permission_basis(store, scope)
        if permission and event.visibility == "group":
            from .group_modes import observing, settings
            from .role_affinity import close_contact

            mode = settings(store, scope)
            if not mode["proactive"] or not observing(store, scope) or not close_contact(store, event):
                return None
            return stable_key("group-matter", permission, mode["version"])
        return permission
    if event.platform == "bilibili" and hasattr(adapter, "reminder_eligible"):
        adapter.reminder_eligible(event)
        return stable_key("private-dialogue", event.thread, event.actor.key)
    return None


def evidence(event, version, quote):
    from .privacy import mask

    return dict(
        event=event.id,
        version=version,
        actor=event.actor.key,
        contact=contact_id(event),
        thread=event.thread,
        at=event.occurred_at,
        assertion=mask(quote),
    )


def pack(store, identifier, data):
    return store.ledger_encode("matter:" + identifier, data)


def retain(store, db, identifier, body):
    from .source_retention import replace

    resources = ["event/" + item["event"] for item in body["sources"]]
    key = "matter-source:" + identifier
    store.set_state(key, True, db)
    replace(db, "state", key, resources)


def unpack(store, row):
    return store.ledger_decode("matter:" + row["id"], row["body"])


def observe(core, event, version, adapter):
    try:
        basis = authority(core.store, event, adapter, outbound=False)
    except AssistantError:
        return
    if not basis:
        return
    store = core.store
    contact = contact_id(event)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO matter_contacts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "anchor=excluded.anchor,occurred=excluded.occurred,permission=excluded.permission "
            "WHERE matter_contacts.occurred<=excluded.occurred",
            (
                contact,
                event.platform,
                event.account,
                event.thread,
                event.actor.key,
                event.id,
                events.utc_time(event.occurred_at),
                basis,
            ),
        )
        watchers = db.execute(
            "SELECT m.* FROM role_matters m JOIN matter_watches w ON m.id=w.matter WHERE "
            "w.contact=? AND m.status IN ('waiting','ready','processing') FOR UPDATE OF m",
            (contact,),
        ).fetchall()
        for row in watchers:
            body = unpack(store, row)
            previous = [r for r in body["sources"] if r["event"] == event.id and r["contact"] == contact]
            if previous and previous[-1]["version"] != version:
                correct_source(store, row, body, event, version, db)


def scoped_text(store, event, entry, governed=None):
    """Revalidate the original disclosure in this exact recipient context before reusing derived words."""
    from .governance import GovernedContext
    from .privacy import check_projection, require_raw

    if not isinstance(entry, dict) or "disclosure" not in entry:
        return ""
    context = governed or GovernedContext(store, event)

    def check():
        check_projection(store, event, entry["pins"])
        pinned = dict(entry["disclosure"])
        lease = context.access.lease(context.request, pinned)
        versions = dict(lease.versions)
        if any(versions.get(key) != revision for key, revision in pinned.items()):
            raise AssistantError("context_changed", "此前材料的来源已变化")
        require_raw(store, context.request, pinned)
        if events.get(store, entry["source"])["version"] != entry["revision"]:
            raise AssistantError("context_changed", "此前材料已更正")

    try:
        check()
    except AssistantError:
        return ""
    if governed is not None:
        governed.resources.update(dict(entry["disclosure"]))
        governed.resources.add("event/" + entry["source"])
        governed.fragment_pins.extend(entry["pins"])
        governed.guards.append(check)
    return entry["text"]


def waiting(store, event, governed=None):
    with store.db() as db:
        rows = db.execute(
            "SELECT m.* FROM role_matters m JOIN matter_watches w ON w.matter=m.id WHERE w.contact=? "
            "AND m.status IN ('ready','waiting','processing') ORDER BY m.id",
            (contact_id(event),),
        ).fetchall()
    items = []
    for row in rows:
        body = unpack(store, row)
        # The addressee may see what the role already asked in this exact conversation.
        label = ""
        prompt = body.get("prompts", {}).get(contact_id(event))
        if prompt:
            label = scoped_text(store, event, prompt, governed)
        if body.get("origin") == contact_id(event):
            from .governance import GovernedContext
            from .privacy import raw_allowed

            try:
                src = latest(store, body["sources"][0])
                access = GovernedContext(store, event)
                resource = "event/" + src.id
                if access.access.can_read(access.request, resource) and raw_allowed(
                    store, access.request, resource
                ):
                    label = body.get("cue", "")
                    if governed is not None:
                        governed.resources.add(resource)
            except AssistantError:
                pass
        items.append(
            {
                "id": row["id"],
                "version": row["version"],
                "previous_message": label,
                "origin": body.get("origin"),
            }
        )
    return items


def correct_source(store, row, body, event, version, db):
    body["sources"].append(evidence(event, version, event.text[:200]))
    retain(store, db, row["id"], body)
    # Discard any decision based on the superseded version. The normal inbox turn
    # can act on the edit, while an existing future alarm remains a role wake.
    wake = body.get("wake_at")
    revised = dict(
        steps=[],
        next="wait",
        wake_at=wake,
        action="alarm" if wake else "wait",
        alarm_note=None,
        idle_wakes=0,
        failures=0,
        new_input=False,
    )
    if row["status"] == "processing":
        body.update(new_input=True, pending_plan=revised)
        db.execute(
            "UPDATE role_matters SET body=?,version=? WHERE id=?",
            (pack(store, row["id"], body), uuid4().hex, row["id"]),
        )
    else:
        body.update(revised)
        update(store, row, body, "waiting", wake or 0, db=db)


def create(store, event, version, now, db):
    contact = contact_id(event)
    anchored = db.execute("SELECT permission FROM matter_contacts WHERE id=?", (contact,)).fetchone()
    if not anchored:
        raise AssistantError("matter_scope", "缺少当前会话依据")
    identifier = stable_key("matter", event.id)
    source = evidence(event, version, event.text[:200])
    body = dict(
        sources=[source],
        origin=contact,
        permission=anchored[0],
        cue=source["assertion"],
        steps=[],
        next="wait",
        wake_at=None,
        new_input=False,
        round=0,
    )
    db.execute(
        "INSERT INTO role_matters VALUES(?,?,'ready','none',?,?,?,NULL,?) ON CONFLICT DO NOTHING",
        (identifier, uuid4().hex, event.platform, event.account, now, pack(store, identifier, body)),
    )
    db.execute("INSERT INTO matter_watches VALUES(?,?) ON CONFLICT DO NOTHING", (identifier, contact))
    retain(store, db, identifier, body)
    return identifier


def latest(store, source):
    record = events.get(store, source["event"])
    if record["version"] != source["version"]:
        raise AssistantError("context_changed", "事项引用的消息已更正")
    return Event.from_dict(record["event"])


def resolve(cores, store, identifier, *, outbound=True):
    with store.db() as db:
        row = db.execute("SELECT * FROM matter_contacts WHERE id=?", (identifier,)).fetchone()
    if not row or row["platform"] not in cores:
        raise AssistantError("matter_scope", "会话未启用")
    core = cores[row["platform"]]
    run = core.guard()
    if run["read_only"] or run.get("reply_test") or [row["platform"], row["account"]] not in run["accounts"]:
        raise AssistantError("matter_scope", "账号不可主动联系")
    event = Event.from_dict(events.get(store, row["anchor"])["event"])
    adapter = core.platforms.get(event.platform, event.account)
    if (
        contact_id(event) != identifier
        or authority(store, event, adapter, outbound=outbound) != row["permission"]
    ):
        raise AssistantError("matter_scope", "会话许可已改变")
    return core, adapter, event, row["permission"]


def candidates(cores, store, after=""):
    # Old, terminal ingress receipts may predate the contacts index. Hydrate
    # on discovery from the original validated event, never from directory names.
    for platform, core in cores.items():
        if platform not in {"qq", "bilibili"}:
            continue
        try:
            accounts = [account for p, account in core.guard()["accounts"] if p == platform]
        except AssistantError:
            continue
        with store.db() as db:
            rows = db.execute(
                "SELECT DISTINCT ON (e.account,e.conversation,e.actor) e.id FROM core_events e "
                "JOIN core_event_receipts r ON e.id=r.event_id AND e.version=r.version "
                "WHERE e.deleted=0 AND e.platform=? AND e.account=ANY(?) AND NOT EXISTS "
                "(SELECT 1 FROM matter_contacts c WHERE c.platform=e.platform AND c.account=e.account "
                "AND c.thread=e.conversation AND c.actor=e.actor) ORDER BY e.account,e.conversation,e.actor,"
                "e.occurred DESC,e.id LIMIT 128",
                (platform, accounts),
            ).fetchall()
        for (identifier,) in rows:
            rec = events.get(store, identifier)
            event = Event.from_dict(rec["event"])
            if platform == "qq" and (
                event.metadata.get("qq", {}).get("identity_quality") != "verified"
                or not store.state("qq-admission:" + identifier)
            ):
                continue
            observe(core, event, rec["version"], core.platforms.get(platform, event.account))
    with store.db() as db:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT id FROM matter_contacts WHERE id>? ORDER BY id LIMIT 17", (after,)
            ).fetchall()
        ]
    result = {}
    for identifier in ids[:16]:
        try:
            _, _, event, basis = resolve(cores, store, identifier)
        except AssistantError:
            continue
        result[identifier] = (event, basis)
    return result, ids[15] if len(ids) > 16 else None


def update(store, row, body, status, wake, *, delivery=None, db):
    touch(db, row["id"])
    db.execute(
        "UPDATE role_matters SET status=?,version=?,attempt=NULL,delivery=?,wake=?,body=? WHERE id=?",
        (status, uuid4().hex, delivery or row["delivery"], wake, pack(store, row["id"], body), row["id"]),
    )
    if status in {"completed", "stopped", "uncertain"}:
        db.execute("DELETE FROM matter_watches WHERE matter=?", (row["id"],))


def advance(store, row, body, now, db, delivery=None):
    if "pending_plan" in body:
        body.update(body.pop("pending_plan"))
        body["new_input"] = False
        if body["steps"]:
            body["recipient"] = body["steps"][0]["target"]
    if body.get("new_input"):
        body["steps"] = []
        update(store, row, body, "ready", now, delivery=delivery, db=db)
    elif body["steps"] or body["next"] == "continue":
        update(store, row, body, "ready", now, delivery=delivery, db=db)
    elif body["next"] == "finish":
        update(store, row, body, "completed", 0, delivery=delivery, db=db)
    elif body["next"] == "stop":
        update(store, row, body, "stopped", 0, delivery=delivery, db=db)
    else:
        update(store, row, body, "waiting", body.get("wake_at") or 0, delivery=delivery, db=db)


def touch(db, identifier):
    db.execute(
        "INSERT INTO role_activity(matter,updated_at) VALUES(?,EXTRACT(EPOCH FROM clock_timestamp())) "
        "ON CONFLICT(matter) DO UPDATE SET updated_at=excluded.updated_at",
        (identifier,),
    )


def claim(core, platform, account):
    now = core.clock().timestamp()
    store = core.store
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM role_matters WHERE platform=? AND account=? AND wake<=? AND "
            "(status IN ('ready','processing') OR (status='waiting' AND wake>0)) ORDER BY wake,id "
            "LIMIT 1 FOR UPDATE SKIP LOCKED",
            (platform, account, now),
        ).fetchone()
        if not row:
            return
        body = unpack(store, row)
        if row["attempt"] or body.get("steps"):
            step_id = body["steps"][0]["id"] if body.get("steps") else None
            action = db.execute(
                "SELECT status,id FROM core_actions WHERE id=? OR dedupe=? "
                "ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1",
                (row["attempt"], step_id, row["attempt"]),
            ).fetchone()
            if action:
                if action[0] == "succeeded":
                    if body["steps"]:
                        body["recipient"] = body["steps"].pop(0)["target"]
                    body["last_result"] = "sent"
                    advance(store, row, body, now, db, "sent")
                else:
                    uncertain = action[0] in {"dispatching", "uncertain"}
                    if uncertain:
                        db.execute(
                            "UPDATE core_actions SET status='uncertain' WHERE id=? AND status='dispatching'",
                            (action[1],),
                        )
                    update(
                        store,
                        row,
                        body,
                        "uncertain" if uncertain else "stopped",
                        0,
                        delivery="uncertain" if uncertain else "cancelled",
                        db=db,
                    )
                return
            if body.get("pending_plan"):
                advance(store, row, body, now, db)
                return
        attempt = uuid4().hex
        version = uuid4().hex
        touch(db, row["id"])
        db.execute(
            "UPDATE role_matters SET status='processing',attempt=?,version=?,wake=? WHERE id=?",
            (attempt, version, now + 300, row["id"]),
        )
        return row["id"], attempt, version, body


def owned(db, identifier, attempt, version=None, *, for_update=False):
    row = db.execute(
        "SELECT * FROM role_matters WHERE id=? AND status='processing' AND attempt=?"
        + (" FOR UPDATE" if for_update else ""),
        (identifier, attempt),
    ).fetchone()
    if not row or version is not None and row["version"] != version:
        raise AssistantError("matter_changed", "事项已停止或有新消息")
    return row
