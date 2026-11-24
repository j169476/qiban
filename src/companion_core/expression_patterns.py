"""Learn new habits from abstract conversational evidence without sharing source text."""
import re
from zoneinfo import ZoneInfo

from jsonschema import validate

from .contracts import stable_key
from .errors import AssistantError
from .schema import obj

# These describe the training signal and when to retrieve it. The habit itself
# is generated in a separate call which cannot see any person's words or name.
SITUATIONS = {
    "time_pressure": ("任务临近、来不及", r"来不及|赶不完|快截止|截止|时间不够|赶时间|赶工|快交|要交|deadline|running out of time"),
    "hesitation": ("犹豫、不知从何着手", r"犹豫|拿不准|不知道怎么|从哪.*开始|纠结|不知所措|not sure"),
    "setback": ("遭遇挫折", r"搞砸|失败|没过|不顺|挫败|又错|摔了|didn't work"),
    "good_news": ("分享好消息", r"成功了|通过了|考过|终于.*了|好消息|拿到了|太开心|good news"),
    "venting": ("倾诉烦恼", r"好烦|好累|烦死|吐槽|难过|委屈|崩溃|想哭|太累|exhausted"),
    "playful": ("轻松玩笑", r"哈哈|开玩笑|整活|接梗|逗你|玩笑|just kidding"),
    "explanation": ("求解、求助", r"帮我|怎么理解|不明白|讲讲|教我|举个例子|帮忙|help me"),
}
MOVES = ["接住情绪", "指出一个具体进展", "征询是否需要建议", "拆出最小动作", "用对方的节奏停顿",
         "先反问一个短问题", "用短句收尾", "给出两个具体选项", "回扣前一句", "先接玩笑再认真", "邀请对方补充"]
TONES = ["简短", "克制", "温和", "直率", "轻快", "沉稳"]
OUTCOMES = ["对方继续说", "对方采纳", "对方纠正", "对方缓和", "明确偏好"]
SIGNAL = obj({"situation": {"enum": list(SITUATIONS)}, "moves": {"type": "array", "minItems": 1, "maxItems": 3,
             "items": {"enum": MOVES}}, "tone": {"enum": TONES}, "outcome": {"enum": OUTCOMES}})
OPTIONAL = {"anyOf": [{"type": "null"}, SIGNAL]}
PROMPT = ("只在确有多轮反应或当事人明确反馈时给出 expression_signal。提取场合、交谈步骤、语气和实际反应；"
          "不把助手自己刚生成的回复当作对方反馈，证据不足留空。")
KEY = "expression-patterns-v1"


def record(store, source, signal, db):
    if not signal:
        return
    validate(signal, SIGNAL)
    evidence = stable_key("example", source)
    state = store.state(KEY, {}, db)
    scene = state.setdefault(signal["situation"], {"samples": [], "trained": []})
    if any(s["key"] == evidence for s in scene["samples"]):
        return
    sample = {"key": evidence, "moves": list(dict.fromkeys(signal["moves"])),
              "tone": signal["tone"], "outcome": signal["outcome"]}
    scene["samples"] = [*scene["samples"], sample][-12:]
    store.set_state(KEY, state, db)


def select(store, text):
    if not text:
        return []
    state = store.state(KEY, {})
    return [state[key]["habit"] for key, (_, pattern) in SITUATIONS.items() if
            state.get(key, {}).get("habit") and re.search(pattern, text[:4000], re.I)][:2]


def synthesize_input(store, day, db):
    if store.state("expression-synthesis-day", None, db) == day:
        return None
    for key, scene in store.state(KEY, {}, db).items():
        fresh = [s for s in scene["samples"] if s["key"] not in scene["trained"]]
        if len(fresh) < 2:
            continue
        store.set_state("expression-synthesis-day", day, db)
        return key, fresh
    return None


async def synthesize(core):
    store = core.store
    if getattr(store, "backend", None) != "postgres":
        return
    # This is a low-priority call shared by all three platforms.
    state = store.state(KEY, {})
    if not any(len({s["key"] for s in entry["samples"]}.difference(entry["trained"])) >= 2
               for entry in state.values()):
        return
    day = core.clock().astimezone(ZoneInfo(store.read("policy")["data"]["timezone"])).date().isoformat()
    if store.state("expression-synthesis-day", None) == day:
        return
    run = core.guard()
    if run["read_only"] or run.get("reply_test"):
        return
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if core.remaining_model_calls() <= 1:
            return
        batch = synthesize_input(store, day, db)
    if not batch:
        return
    key, fresh = batch
    context = {"situation": SITUATIONS[key][0], "observations": [
        {k: s[k] for k in ("moves", "tone", "outcome")} for s in fresh]}
    prompt = ("根据这些独立交流的抽象观察，归纳一条可以由角色按自己人设运用的新表达习惯。"
              "用自己的话写具体时机、先后次序和收住的条件。只写做法，不写示例台词、姓名、地点或个人事实。"
              "反应相矛盾或还看不出规律时返回 null。")
    schema = obj({"habit": {"anyOf": [{"type": "null"}, {"type": "string", "minLength": 6, "maxLength": 96}]}})
    try:
        result = await core.model(prompt, context, schema, guard=lambda: core.guard(run["generation"]), reserve_for_replies=1)
    except AssistantError:
        return
    habit = result["habit"]
    if habit and (re.search(r'[\d@#:/\\\n“”「」【】<>"]|账号|用户名|微信号|住址|授权|工具调用', habit)):
        habit = None
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        core.guard(run["generation"])
        state = store.state(KEY, {}, db)
        scene = state.get(key)
        keys = [s["key"] for s in fresh]
        if scene is None or not set(keys) <= {s["key"] for s in scene["samples"]}:
            return
        scene["trained"] = [*scene["trained"], *keys][-24:]
        if habit:
            scene["habit"] = habit.strip()
        store.set_state(KEY, state, db)
