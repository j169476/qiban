"""Durable reconsideration of events whose proposed action was certainly not sent."""
import json
from datetime import datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

TEMPORARY = {
    "interval", "quota_exhausted", "reply_loop_limit", "model_quota", "outside_hours",
    "before_send_cancelled", "core_before_send_cancelled", "policy_changed", "context_changed",
    "run_suspended", "not_running", "stale_generation", "chat_archived", "duration_complete",
}
MAX_AGE = timedelta(days=3)
MAX_DEFERRALS = 5


def key(event):
    return "core-delivery:" + event.id


def config_token(store):
    values = {kind: store.read(kind)["version"]
              for kind in ("manifest", "policy", "preferences", "persona", "companion")}
    return sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def finish(core, event, version, run, reason="completed"):
    with core.store.lock, core.store.db() as db:
        source = db.execute("SELECT version,deleted FROM core_events WHERE id=?", (event.id,)).fetchone()
        if not source or source["deleted"] or source["version"] != version:
            return {"status": "superseded"}
        old = core.store.state(key(event), {}, db)
        core.store.set_state(key(event), {**old, "owner": run["owner"], "version": version,
                             "status": "completed", "reason": reason,
                             "updated_at": core.clock().isoformat()}, db)
        core.store.set_state("core-processed:" + event.id, version, db)


def eligible(core, event, version, run):
    record = core.store.state(key(event))
    if not record or record.get("version") != version:
        return True
    if record["status"] != "deferred" or record["owner"] != run["owner"]:
        return False
    if core.clock() - datetime.fromisoformat(event.occurred_at) > MAX_AGE:
        finish(core, event, version, run, "expired")
        return False
    if record.get("attempts", 0) >= MAX_DEFERRALS:
        finish(core, event, version, run, "retry_limit")
        return False
    return (core.clock() >= datetime.fromisoformat(record["due_at"])
            or record.get("config_token") != config_token(core.store))


def defer(core, event, version, run, reason):
    now = core.clock()
    policy = core.store.read("policy")["data"]
    if reason in {"quota_exhausted", "reply_loop_limit", "model_quota"}:
        local = now.astimezone(ZoneInfo(policy["timezone"]))
        due = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif reason == "interval":
        due = now + timedelta(seconds=max(1, policy["min_action_interval_seconds"]))
    elif reason == "outside_hours":
        local = now.astimezone(ZoneInfo(policy["timezone"]))
        hour, minute = map(int, policy["active_hours"]["start"].split(":"))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= local:
            due += timedelta(days=1)
    else:
        due = now + timedelta(seconds=30)
    with core.store.lock, core.store.db() as db:
        source = db.execute("SELECT version,deleted FROM core_events WHERE id=?", (event.id,)).fetchone()
        if not source or source["deleted"] or source["version"] != version:
            return {"status": "superseded"}
        old = core.store.state(key(event), {}, db)
        attempts = old.get("attempts", 0) if old.get("version") == version else 0
        record = {"owner": run["owner"], "version": version, "status": "deferred", "reason": reason,
                  "attempts": attempts + 1, "due_at": due.isoformat(), "updated_at": now.isoformat(),
                  "config_token": config_token(core.store)}
        core.store.set_state(key(event), record, db)
    return record


def retryable(row, store=None):
    """A receipt with an explicit pre-send cancellation can release its dedupe key."""
    result = (store.ledger_decode("core_actions.result", row["result"]) if store and row["result"]
              else json.loads(row["result"] or "{}"))
    return row["status"] == "cancelled" and result.get("error") in TEMPORARY
