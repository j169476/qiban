"""Per-account delivery budgets; local conversation is a separate policy field."""
from zoneinfo import ZoneInfo

from .contracts import stable_key
from .errors import AssistantError


def status(core, adapter, db=None):
    policy = core.store.read("policy")["data"]
    day = core.clock().astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
    field = getattr(adapter, "action_budget_field", "daily_action_limit")
    default = policy["daily_model_cycles"] if field == "daily_local_replies" else 20
    key = stable_key("action-budget", adapter.platform, adapter.account, day)
    used = core.store.state(key, 0, db)
    limit = policy.get(field, default)
    return {"key": key, "day": day, "field": field, "used": used, "limit": limit,
            "remaining": max(0, limit - used)}


def require(core, adapter, db=None):
    value = status(core, adapter, db)
    if not value["remaining"]:
        raise AssistantError("quota_exhausted", "当前入口今日回复额度已用完")
    return value
