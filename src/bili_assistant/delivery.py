"""Durable reply deferral. A due event is reconsidered, never blindly resent."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TEMPORARY = {"interval", "quota_exhausted", "reply_loop_limit", "before_send_cancelled"}
MAX_AGE = timedelta(days=3)
MAX_DEFERRALS = 5


def key(account, event_id):
    return f"delivery:{account}:{event_id}"


def config_versions(store):
    return {kind: store.read(kind)["version"] for kind in ("policy", "preferences", "persona", "companion")}


def recover(engine):
    """Recover pre-0.4.3 skipped drafts after an explicit start, once per generation."""
    run = engine.role_run()
    if run.get("read_only"):
        return
    marker = f"delivery-recovered:{run['generation']}"
    with engine.store.lock, engine.store.db() as db:
        if engine.store.state(marker, db=db):
            return
        rows = db.execute(
            """SELECT a.payload FROM actions a WHERE a.account=? AND a.owner=?
               AND a.kind IN ('reply','dm_reply') AND a.status IN ('prepared','cancelled')
               AND a.dedupe NOT LIKE '%:retired:%'""", (run["account"], run["owner"]),
        ).fetchall()
        for row in rows:
            event_id = engine.store.ledger_decode("actions.payload", row[0]).get("event_id")
            if not event_id or engine.store.state(key(run["account"], event_id), db=db):
                continue
            event = db.execute("SELECT received FROM events WHERE account=? AND id=?",
                               (run["account"], event_id)).fetchone()
            if not event or datetime.fromisoformat(event[0]) < datetime.fromisoformat(run["pending_since"]):
                continue
            engine.store.set_state(f"processed:{run['account']}:{event_id}", False, db)
            engine.store.set_state(key(run["account"], event_id), {
                "event_id": event_id, "owner": run["owner"], "status": "deferred", "reason": "unsent_recovered",
                "attempts": 0, "due_at": engine.clock().isoformat(), "updated_at": engine.clock().isoformat(),
                "config_versions": {},
            }, db)
        engine.store.set_state(marker, True, db)


def finish(engine, event, reason="completed"):
    run = engine.role_run()
    with engine.store.lock, engine.store.db() as db:
        old = engine.store.state(key(run["account"], event["id"]), {}, db)
        engine.store.set_state(key(run["account"], event["id"]), {
            **old, "event_id": event["id"], "owner": run["owner"], "status": "completed",
            "reason": reason, "updated_at": engine.clock().isoformat(),
        }, db)
        engine.store.set_state(f"processed:{run['account']}:{event['id']}", True, db)


def eligible(engine, event):
    run = engine.role_run()
    record = engine.store.state(key(run["account"], event["id"]))
    if not record:
        return True
    if record["status"] != "deferred" or record["owner"] != run["owner"]:
        return False
    if engine.clock() - datetime.fromisoformat(event["received"]) > MAX_AGE:
        finish(engine, event, "expired")
        return False
    if record.get("attempts", 0) >= MAX_DEFERRALS:
        finish(engine, event, "retry_limit")
        return False
    return (record.get("reason") == "outside_hours"  # Compatibility with unsent pre-0.9.1 waits.
            or engine.clock() >= datetime.fromisoformat(record["due_at"])
            or record.get("config_versions") != config_versions(engine.store))


def defer(engine, event, reason):
    run = engine.role_run()
    now = engine.clock()
    policy = engine.store.read("policy")["data"]
    if reason in ("quota_exhausted", "reply_loop_limit"):
        local = now.astimezone(ZoneInfo(policy["timezone"]))
        due = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif reason == "interval":
        with engine.store.db() as db:
            last = db.execute("SELECT MAX(dispatched) FROM actions WHERE account=?", (run["account"],)).fetchone()[0]
        due = max(now + timedelta(seconds=1), datetime.fromisoformat(last) + timedelta(
            seconds=policy["min_action_interval_seconds"])) if last else now + timedelta(seconds=1)
    else:
        due = now + timedelta(seconds=30)
    with engine.store.lock, engine.store.db() as db:
        old = engine.store.state(key(run["account"], event["id"]), {}, db)
        record = {
            "event_id": event["id"], "owner": run["owner"], "status": "deferred", "reason": reason,
            "attempts": old.get("attempts", 0) + 1, "due_at": due.isoformat(),
            "updated_at": now.isoformat(), "config_versions": config_versions(engine.store),
        }
        engine.store.set_state(key(run["account"], event["id"]), record, db)
        engine.store.set_state(f"processed:{run['account']}:{event['id']}", False, db)
    return record


def status(store, account):
    rows = list(store.states(f"delivery:{account}:%").values())
    pending = [row for row in rows if row["status"] == "deferred"]
    return {"pending": len(pending), "recent": sorted(rows, key=lambda r: r["updated_at"], reverse=True)[:20]}


def next_delay(engine, default):
    run = engine.run(check_host=False)
    if run.get("read_only"):
        return default
    rows = list(engine.store.states(f"delivery:{run['account']}:%").values())
    waits = [(datetime.fromisoformat(row["due_at"]) - engine.clock()).total_seconds()
             for row in rows if row["status"] == "deferred" and row["owner"] == run["owner"]]
    return max(30, min(waits)) if waits else default
