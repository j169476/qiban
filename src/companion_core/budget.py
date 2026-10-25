"""One daily model budget, including the receipts written by older controllers."""
from zoneinfo import ZoneInfo

from .errors import AssistantError


def _usage(store, day, db):
    key = "model-budget:" + day
    record = store.state(key, {"used": 0, "sources": {}}, db)
    # Imported counters are monotonic high-water marks. Mirroring a new reservation
    # updates its mark in the same transaction, so migration never counts it twice.
    sources = dict(record["sources"])
    used = record["used"]
    for row in db.execute("SELECT key,value FROM state WHERE key LIKE ? OR key=?",
                          ("model-cycles:%:" + day, "core-model:" + day)):
        value = store.ledger_decode("state:" + row["key"], row["value"])
        if type(value) is not int or value < 0:
            raise AssistantError("model_budget_invalid", "历史模型额度记录无效，需在管理入口检查")
        previous = sources.get(row["key"], 0)
        used += max(0, value - previous)
        sources[row["key"]] = max(previous, value)
    return key, {"used": used, "sources": sources}


def remaining(store, policy, now):
    day = now.astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        key, record = _usage(store, day, db)
        store.set_state(key, record, db)
    return max(0, policy["daily_model_cycles"] - record["used"])


def status(store, policy, now):
    """Report the same effective ledger without changing accounting state."""
    day = now.astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
    with store.lock, store.db() as db:
        _, record = _usage(store, day, db)
    return {"day": day, "scope": "data_space", "used": record["used"],
            "limit": policy["daily_model_cycles"],
            "remaining": max(0, policy["daily_model_cycles"] - record["used"])}


def reserve(store, policy, now, *, account=None):
    """Charge before calling the model; cancellation does not refund a started call."""
    day = now.astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
    source = f"model-cycles:{account}:{day}" if account is not None else "core-model:" + day
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        key, record = _usage(store, day, db)
        if record["used"] >= policy["daily_model_cycles"]:
            raise AssistantError("model_quota", "当日模型额度已用完")
        value = max(store.state(source, 0, db), record["sources"].get(source, 0)) + 1
        record["used"] += 1
        record["sources"][source] = value
        store.set_state(source, value, db)
        store.set_state(key, record, db)
    return record["used"]
