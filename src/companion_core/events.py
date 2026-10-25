"""Durable normalized events; evidence always refers to an exact event version."""
import hashlib
import json
from datetime import datetime, timezone

from .contracts import Event
from .errors import AssistantError


def utc_time(value):
    """Canonical query time; retain the original representation in the evidence body."""
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("event time requires a timezone")
        return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        raise AssistantError("invalid_event_time", "事件时间需要有效的 ISO 时间及明确时区") from None


def ingest(store, event):
    occurred = utc_time(event.occurred_at)
    body = json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True)
    version = hashlib.sha256(body.encode()).hexdigest()
    with store.lock, store.db() as db:
        old = db.execute("SELECT version,deleted FROM core_events WHERE id=?", (event.id,)).fetchone()
        if old and old[1]:
            return {"id": event.id, "version": old[0], "new": False, "changed": False, "deleted": True}
        db.execute("""INSERT INTO core_events VALUES (?,?,?,?,?,?,?,?,0)
            ON CONFLICT(id) DO UPDATE SET platform=excluded.platform,account=excluded.account,
                actor=excluded.actor,conversation=excluded.conversation,occurred=excluded.occurred,
                body=excluded.body,version=excluded.version,deleted=0""",
             (event.id, event.platform, event.account, event.actor.key, event.thread, occurred,
              store.ledger_encode("core_events.body", event.as_dict()), version))
        from .governance import enabled, register_event

        if enabled(store) and (old is None or old[0] != version):
            register_event(store, event, version, db=db)
            if getattr(store, "backend", None) == "postgres":
                from .access import RequestAuthority
                from .governance import GovernanceRepository
                from .relations import RelationService

                RelationService(GovernanceRepository(store), RequestAuthority()).register_actor(event.actor)
    return {"id": event.id, "version": version, "new": old is None,
            "changed": bool(old and (old[0] != version or old[1]))}


def get(store, event_id):
    with store.db() as db:
        row = db.execute("SELECT body,version FROM core_events WHERE id=? AND deleted=0", (event_id,)).fetchone()
    if not row:
        raise AssistantError("unknown_evidence", "身份依据的事件不存在或已删除")
    return {"event": store.ledger_decode("core_events.body", row[0]), "version": row[1]}


def forget(store, event_id):
    with store.lock, store.db() as db:
        db.execute("UPDATE core_events SET deleted=1 WHERE id=?", (event_id,))
        from .governance import GovernanceRepository, enabled

        if enabled(store):
            from dataclasses import replace

            repository = GovernanceRepository(store)
            policy = repository.policy("event/" + event_id)
            if policy:
                repository.set_policy(replace(policy, valid=False, version=hashlib.sha256(
                    (policy.version + ":deleted").encode()).hexdigest()), policy.version, "source-deleted", db=db)


def recent(store, *, actor=None, conversation=None, limit=12):
    where, args = ["deleted=0"], []
    if actor:
        where.append("actor=?")
        args.append(actor.key)
    if conversation:
        where.append("conversation=?")
        args.append(conversation)
    with store.db() as db:
        ordering = "occurred::timestamptz" if getattr(store, "backend", None) == "postgres" else "julianday(occurred)"
        rows = db.execute("SELECT body,version FROM core_events WHERE " + " AND ".join(where)
                          + f" ORDER BY {ordering} DESC,id DESC LIMIT ?",
                          (*args, min(50, max(1, limit)))).fetchall()
    return [{"event": store.ledger_decode("core_events.body", row[0]), "version": row[1]} for row in reversed(rows)]


def pending(store, accounts, since, limit=100, *, owner=None, now=None, config_token=None):
    """Read the durable scoped inbox without comparing offset-bearing timestamps as text.

    SQLite's date conversion also reads pre-0.5.1 columns correctly, so opening an
    old store does not require rewriting evidence or discarding consumption receipts.
    """
    if not accounts or limit <= 0:
        return []
    cutoff = utc_time(since)
    scope = " OR ".join("(e.platform=? AND e.account=?)" for _ in accounts)
    args = [part for pair in accounts for part in pair]
    if getattr(store, "backend", None) == "postgres":
        return _pending_postgres(store, scope, args, cutoff, limit, owner, now, config_token)
    ready, ready_args = "", []
    if owner is not None and now is not None:
        # Apply scheduling before LIMIT so sleeping events cannot crowd out a new
        # message in another conversation. Changed sources/configuration wake up.
        ready = """AND NOT EXISTS (SELECT 1 FROM state d WHERE d.key='core-delivery:' || e.id
            AND json_extract(d.value,'$.version')=e.version AND (
                json_extract(d.value,'$.owner')!=? OR json_extract(d.value,'$.status')='completed'
                OR (json_extract(d.value,'$.status')='deferred'
                    AND julianday(json_extract(d.value,'$.due_at'))>julianday(?)
                    AND json_extract(d.value,'$.config_token')=?))) """
        ready_args = [owner, utc_time(now), config_token]
    with store.db() as db:
        rows = db.execute(
            "SELECT e.body FROM core_events e WHERE e.deleted=0 AND (" + scope + ") "
            "AND julianday(e.occurred)>=julianday(?) AND NOT EXISTS ("
            "SELECT 1 FROM state s WHERE s.key='core-processed:' || e.id AND s.value=json_quote(e.version)) "
            + ready + "ORDER BY julianday(e.occurred),e.id LIMIT ?",
            (*args, cutoff, *ready_args, min(1000, limit)),
        ).fetchall()
    return [Event.from_dict(json.loads(row[0])) for row in rows]


def _pending_postgres(store, scope, args, cutoff, limit, owner, now, config_token):
    ready, ready_args = "", []
    if owner is not None and now is not None:
        ready = """AND NOT EXISTS (SELECT 1 FROM core_delivery_state d WHERE d.event_id=e.id
            AND d.version=e.version AND (d.owner!=? OR d.status='completed' OR
                (d.status='deferred' AND d.due_at>?::timestamptz AND d.config_token=?))) """
        ready_args = [owner, utc_time(now), config_token]
    with store.db() as db:
        rows = db.execute("SELECT e.body FROM core_events e WHERE e.deleted=0 AND (" + scope + ") "
                          "AND e.occurred::timestamptz>=?::timestamptz AND NOT EXISTS ("
                          "SELECT 1 FROM core_event_receipts s WHERE s.event_id=e.id AND s.version=e.version) "
                          + ready + "ORDER BY e.occurred::timestamptz,e.id LIMIT ?",
                          (*args, cutoff, *ready_args, min(1000, limit))).fetchall()
    return [Event.from_dict(store.ledger_decode("core_events.body", row[0])) for row in rows]
