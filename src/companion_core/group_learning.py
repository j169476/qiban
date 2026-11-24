"""Bounded listening batches. Only anonymous, schema-constrained observations leave the group."""
from dataclasses import asdict
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import expression_learning, expression_patterns, group_modes
from .contracts import stable_key
from .errors import AssistantError
from .reply_authorization import ReplyScope
from .schema import obj

MIN_SAMPLES = 4
MAX_SAMPLES = 8
MAX_QUEUED_GROUPS = 16
GLOBAL_DAILY = 4
GROUP_DAILY = 2
INTERVAL = 3600
SCHEMA = obj({"cue": {"enum": [None, *expression_learning.HABITS]}})
SCHEMA["properties"]["expression_signal"] = expression_patterns.OPTIONAL
PROMPT = ("这些消息是一个群内按时间排列的短对话。只根据成员之间的来回反应，判断一种值得角色学习的表达技巧。"
          "不要把某个人的口头禅、私事或转述当成公共经验。材料不足时返回 null。"
          "只返回给定结构内的匿名观察。你在旁听，没有发送消息或创建任务的能力。")


def sample(store, scope, event, stamp, db):
    key = "group-samples:" + scope.id
    mode = group_modes.settings(store, scope, db)
    batch = store.state(key, {}, db)
    if batch.get("version") != mode["version"]:
        batch = {"version": mode["version"], "items": [], "next": 0}
    if stamp < batch["next"]:
        return
    items = [r for r in batch["items"] if stamp - r["at"] <= INTERVAL]
    if len(items) >= MAX_SAMPLES or any(r["id"] == event.id for r in items):
        return
    items.append({"actor": event.actor.key, "id": event.id, "at": stamp, "text": event.text[:180]})
    batch["items"] = items
    store.set_state(key, batch, db)
    if len(items) >= MIN_SAMPLES and len({r["actor"] for r in items}) >= 2:
        queue_key = stable_key("group-learning-queue", event.platform, event.account)
        queue = store.state(queue_key, [], db)
        if asdict(scope) not in queue and len(queue) < MAX_QUEUED_GROUPS:
            store.set_state(queue_key, [*queue, asdict(scope)], db)


async def tick(core, platform, account):
    store = core.store
    if platform not in ("qq", "wechat") or getattr(store, "backend", None) != "postgres":
        return
    queue_key = stable_key("group-learning-queue", platform, account)
    if not store.state(queue_key, []):
        return
    run = core.guard()
    if run["read_only"] or run.get("reply_test"):
        return
    now = core.clock()
    stamp = now.timestamp()
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        queue = store.state(queue_key, [], db)
        if not queue:
            return
        scope = ReplyScope(**queue[0])
        key = "group-samples:" + scope.id
        batch = store.state(key, {}, db)
        mode = group_modes.settings(store, scope, db)
        items = [r for r in batch.get("items", []) if 0 <= stamp - r["at"] <= INTERVAL
                 and group_modes.observing(store, scope, r["at"], db)]
        store.set_state(queue_key, queue[1:], db)
        day = now.astimezone(ZoneInfo(store.read("policy")["data"]["timezone"])).date().isoformat()
        budget = store.state("group-learning-day", {}, db)
        used = budget.get("used", 0) if budget.get("day") == day else 0
        group_used = batch.get("used", 0) if batch.get("day") == day else 0
        eligible = (mode["version"] == batch.get("version") and mode["listen"]
                    and len(items) >= MIN_SAMPLES and len({r["actor"] for r in items}) >= 2
                    and used < GLOBAL_DAILY and group_used < GROUP_DAILY and core.remaining_model_calls() > 1)
        token = uuid4().hex
        store.set_state(key, {"version": mode["version"], "token": token, "next": stamp + INTERVAL,
                              "items": [], "day": day, "used": group_used + int(eligible)}, db)
        if not eligible:
            return
        store.set_state("group-learning-day", {"day": day, "used": used + 1}, db)

    def guard():
        core.guard(run["generation"])
        if (store.state(key, {}).get("token") != token or group_modes.settings(store, scope)["version"] != mode["version"]
                or not all(group_modes.observing(store, scope, r["at"]) for r in items)):
            raise AssistantError("context_changed", "旁听授权已经改变")

    speakers = list(dict.fromkeys(r["actor"] for r in items))
    context = {"turns": [{"speaker": speakers.index(r["actor"]) + 1, "text": r["text"]} for r in items],
               "choices": {k: v[0] for k, v in expression_learning.HABITS.items()}}
    try:
        decision = await core.model(PROMPT + expression_patterns.PROMPT, context, SCHEMA, guard=guard, reserve_for_replies=1)
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            guard()
            source = stable_key("group-batch", *[r["id"] for r in items])
            expression_learning.observe(store, platform, scope.id, source, "", db, decision["cue"])
            expression_patterns.record(store, source, decision.get("expression_signal"), db)
    except AssistantError:
        # Optional learning yields to ordinary replies and never replays a charged batch.
        return
