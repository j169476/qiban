"""Administrative runtime metadata, separate from conversation contents."""
import time

from companion_core import action_budget, budget
from companion_core.observability import TOKENS, report


def snapshot(service, *, days=1):
    observed = report(service.store, days=days, limit=30)
    models = [group for group in observed["groups"] if group["kind"] == "model"]
    tokens = {}
    for key in TOKENS:
        known = [group["known_tokens"][key] for group in models if group["known_tokens"][key] is not None]
        tokens[key] = sum(known) if known else None
    policy = service.store.read("policy")["data"]
    quotas = [{"id": "model", "label": "模型调用", **budget.status(service.store, policy, service.core.clock())},
              {"id": "local", "label": "本机会客室回复", **action_budget.status(service.core, service.local)}]
    for platform, core in service.platform_runs.cores.items():
        for adapter in core.platforms.all():
            quotas.append({"id": platform, "label": {"bilibili": "B 站互动", "qq": "QQ 发送", "wechat": "微信发送"}[platform],
                           **action_budget.status(core, adapter)})
    for item in quotas:
        item.pop("key", None)
    tasks = []
    with service.store.db() as db:
        for row in db.execute("SELECT key,value FROM state WHERE key LIKE ? ORDER BY key", ("explicit-task:%",)):
            state = service.store.state(row["key"], {}, db)
            tasks.append({"id": row["key"].split(":", 1)[1], **{key: state.get(key) for key in
                          ("status", "phase", "step", "error", "revision")}})
    tasks.sort(key=lambda task: task["status"] not in ("queued", "running"))
    tasks = tasks[:100]
    plan = service.plan_usage.snapshot() if service.plan_usage else {
        "status": "unavailable", "updated_at": None, "stale": True, "buckets": [], "scope": "account"}
    return {"updated_at": time.time(), "timezone": policy["timezone"], "quotas": quotas,
            "coding_plan": plan, "tokens": tokens, "model_calls": sum(g["calls"] for g in models),
            "token_known_calls": {key: sum(g["token_known_calls"][key] for g in models) for key in TOKENS},
            "days": days, "since": observed["since"], "recent": observed["recent"], "tasks": tasks,
            "notes": observed["notes"], "write_failures": observed["write_failures_this_process"]}
