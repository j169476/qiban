"""Small, grounded emotional changes with decay, daily caps and crash-safe receipts."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from .conversations import identifier
from .errors import AssistantError
from .schema import EMOTION_CHANGE


def current(engine):
    store = engine.store
    account = store.read("manifest")["data"]["account_uid"]
    growth = store.read("growth")["data"]
    settings = store.read("companion")["data"]
    now = engine.clock()
    day = now.astimezone(ZoneInfo(store.read("policy")["data"]["timezone"])).date().isoformat()
    old = growth.get("emotion")
    neutral = {
        "label": "平静", "reason": "没有新的情绪依据",
        "valence": 0.0, "arousal": 0.35, "updated_at": now.isoformat(),
        "account_uid": account or "0", "event_id": "baseline", "day": day, "change_today": 0.0,
    }
    if not old or old["account_uid"] != account:
        return neutral
    minutes = max(0, (now - datetime.fromisoformat(old["updated_at"])).total_seconds() / 60)
    weight = 0.5 ** (minutes / settings.get("emotion_half_life_minutes", 180))
    valence = round(old["valence"] * weight, 6)
    arousal = round(0.35 + (old["arousal"] - 0.35) * weight, 6)
    calm = abs(valence) + abs(arousal - 0.35) < 0.025
    return {
        **old, "valence": valence, "arousal": arousal,
        "label": "平静" if calm else old["label"],
        "reason": "经过一段间隔，短期情绪已逐渐回到平稳" if calm else old["reason"],
        "day": day, "change_today": old["change_today"] if old["day"] == day else 0.0,
    }


def evidence_key(account, evidence_id):
    return "emotion-seen:" + identifier("source", account, evidence_id)


def evolve(before, proposal, settings, now, event_id):
    """Shared deterministic limits for public and private emotional changes."""
    step = settings.get("emotion_step_limit", 0.2)
    deltas = [max(-step, min(step, proposal[key])) for key in ("valence_delta", "arousal_delta")]
    cost = sum(abs(value) for value in deltas)
    remaining = max(0, settings.get("emotion_daily_change", 1.2) - before["change_today"])
    scale = min(1, remaining / cost) if cost else 0
    if not scale:
        return dict(before)
    return {**before, "label": proposal["label"], "reason": proposal["reason"],
        "valence": round(max(-1, min(1, before["valence"] + deltas[0] * scale)), 6),
        "arousal": round(max(0, min(1, before["arousal"] + deltas[1] * scale)), 6),
        "change_today": round(before["change_today"] + cost * scale, 6),
        "event_id": event_id, "updated_at": now.isoformat()}


def available(engine, evidence_ids):
    account = engine.store.read("manifest")["data"]["account_uid"]
    return [item for item in evidence_ids if not engine.store.state(evidence_key(account, item))]


def recover(engine):
    store = engine.store
    account = store.read("manifest")["data"]["account_uid"]
    key = f"emotion-pending:{account}"
    with store.lock:
        pending = store.state(key)
        if not pending:
            return
        growth = store.read("growth")
        doc = pending["event"]
        if growth["data"].get("emotion", {}).get("event_id") != doc["id"]:
            if growth["version"] != pending["growth_version"]:
                store.audit("learning", {"event": "emotion_conflict", "id": doc["id"]})
                store.set_state(key, None)
                return
            growth["data"]["emotion"] = doc["after"]
            store.write("growth", growth["data"], growth["version"], "emotion-update")
        if not store.exists("emotion_events", doc["id"]):
            store.write("emotion_events", doc, None, "emotion-update", doc["id"])
        with store.db() as db:
            for item in doc["proposal"]["evidence_ids"]:
                store.set_state(evidence_key(account, item), doc["id"], db)
            store.set_state(key, None, db)


def apply(engine, proposal, allowed, expected_version):
    engine.mode("role")
    if list(Draft202012Validator(EMOTION_CHANGE).iter_errors(proposal)):
        raise AssistantError("invalid_emotion", "情绪变化不符合结构化协议")
    if len(set(proposal["evidence_ids"])) != len(proposal["evidence_ids"]):
        raise AssistantError("invalid_emotion", "情绪依据不能重复")
    if not set(proposal["evidence_ids"]) <= set(allowed):
        raise AssistantError("ungrounded_emotion", "情绪必须引用当前实际读取的互动、视频或角色消息")
    store = engine.store
    account = store.read("manifest")["data"]["account_uid"]
    if not account:
        raise AssistantError("account_not_bound", "没有已绑定账号")
    with store.lock:
        recover(engine)
        settings = store.read("companion")["data"]
        if not settings.get("emotion_enabled", True):
            return {"applied": False, "reason": "disabled", "current": current(engine)}
        if any(store.state(evidence_key(account, item)) for item in proposal["evidence_ids"]):
            return {"applied": False, "reason": "duplicate", "current": current(engine)}
        growth = store.read("growth")
        if growth["version"] != expected_version:
            raise AssistantError("context_changed", "情绪更新期间成长文件已经变化，请重新读取")
        before = current(engine)
        event_id = identifier("emotion", account, sorted(proposal["evidence_ids"]))
        after = evolve(before, proposal, settings, engine.clock(), event_id)
        if after == before:
            return {"applied": False, "reason": "daily_cap_or_no_change", "current": before}
        event = {
            "schema_version": 1, "id": event_id, "account_uid": account,
            "content": proposal["reason"], "proposal": proposal, "before": before, "after": after,
            "source": {"type": "reflection", "recorded_at": engine.clock().isoformat(),
                       "reference": ",".join(proposal["evidence_ids"])},
            "locked": False,
        }
        store.set_state(f"emotion-pending:{account}", {"event": event, "growth_version": expected_version})
        recover(engine)
        return {"applied": True, "event_id": event_id, "current": after}
