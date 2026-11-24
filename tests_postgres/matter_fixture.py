"""Synthetic records for a fresh isolated desktop management QA database."""

from datetime import datetime, timezone

from companion_core import events, privacy
from companion_core.contracts import ActorRef, Event
from companion_core.governance import GovernanceRepository


def legacy_alarm(store, event, due, clue, source="explicit"):
    """Write the v19 record format from a real, already-admitted synthetic event."""
    from uuid import uuid4

    from companion_core import reminders
    from companion_core.contracts import stable_key
    from companion_core.source_retention import replace

    record = events.get(store, event.id)
    event = Event.from_dict(record["event"])
    scope = reminders.scope_for(store, event)
    identifier = f"{int(datetime.fromisoformat(event.occurred_at).timestamp()*1000000):018d}-" + stable_key("alarm", event.id)
    payload = {"clue":clue,"source":source,"source_event":event.id,"source_version":record["version"],
               "target":scope.target_id if scope else event.actor.user_id,"kind":event.visibility,
               "permission":reminders.permission_basis(store,scope) if scope else None}
    if source == "care":
        payload["care_permission"] = reminders.care_permission(store,event)
    with store.db() as db:
        db.execute("INSERT INTO role_reminders(id,scope,platform,account,due,status,version,body) "
                   "VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT DO NOTHING",
                   (identifier,reminders.owner(event),event.platform,event.account,due.timestamp(),uuid4().hex,
                    store.ledger_encode("role-reminder:"+identifier,payload)))
        hold = "alarm-source:"+identifier
        store.set_state(hold,event.id,db)
        replace(db,"state",hold,["event/"+event.id])
    return reminders.listing(store,id=identifier)["items"][0]


def bound_turn(text=None, *, recipient=None):
    args = {"text":text, **({"recipient":recipient} if recipient is not None else {})}
    return {"action":{"capability":"message.reply","arguments":args} if text else None,"continuation":None}


def seed(store):
    if not getattr(store, "database", None):
        raise ValueError("Expected an isolated PostgreSQL test store")
    stamp = datetime.now(timezone.utc).isoformat()
    for who in ("fixture-one", "fixture-two", "fixture-three"):
        credential = who == "fixture-three"
        event = Event(
            "qq",
            "fixture-account",
            who,
            ActorRef("qq", who),
            "fixture-group",
            stamp,
            "合成场次" if not credential else "令牌=synthetic-fixture",
            "group",
            metadata={"qq": {"target_id": "fixture-group"}},
        )
        rec = events.ingest(store, event)
        resource = "event/" + event.id
        policy = GovernanceRepository(store).policy(resource)
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            privacy.add_slice(
                store,
                db,
                {"resource": resource, "source_version": rec["version"]},
                0,
                actor=event.actor.key,
                thread=event.thread,
                visibility="group",
                at=stamp,
                policy=policy.version,
                dependencies=[],
                text=privacy.MASK if credential else event.text,
                value="" if credential else event.text,
                category="credential" if credential else "meeting",
                level="credential" if credential else "circle",
                subject="",
                evidence="uncertain" if credential else "natural",
                until=None,
            )
            db.execute("UPDATE privacy_jobs SET complete=1 WHERE resource=?", (resource,))
    with store.db() as db:
        from companion_core.role_matters import contact_id, touch
        contact = contact_id(event)
        db.execute("INSERT INTO matter_contacts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                   (contact,event.platform,event.account,event.thread,event.actor.key,event.id,stamp,"fixture-only"))
        wake = datetime.now(timezone.utc).timestamp()+3600
        db.execute(
            "INSERT INTO role_matters VALUES(?,?,'waiting','sent',?,?,?,NULL,?) ON CONFLICT DO NOTHING",
            (
                "fixture-matter",
                "fixture-version",
                "qq",
                "fixture-account",
                wake, store.ledger_encode("matter:fixture-matter", {"action":"alarm","recipient":contact,"wake_at":wake}),
            ),
        )
        touch(db, "fixture-matter")
