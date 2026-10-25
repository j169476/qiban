"""Goal state, version validity, withheld-answer assessment and spaced review contracts."""
import hashlib
import json
from copy import deepcopy

from .cognition import BOUNDARIES, stable_id, text, verify_versions
from .errors import AssistantError
from .knowledge import disputed_knowledge, same_versions
from .schema import ANSWER, GRADE, QUESTION, obj
from .storage import recorded_time

QUIZ_SCHEMA = obj({"questions": {"type": "array", "items": QUESTION, "minItems": 2, "maxItems": 8}})

ANSWER_SCHEMA = obj({"answers": {"type": "array", "items": ANSWER, "minItems": 2, "maxItems": 8}})

GRADE_SCHEMA = obj({
    "grades": {"type": "array", "items": GRADE, "minItems": 2, "maxItems": 8},
    "limitations": text(2000),
})

QUIZ_PROMPT = BOUNDARIES + (
    "用实际 sources 为目标出 2 至 8 道检验理解的题，覆盖 criteria 的每一个零基索引。"
    "至少一道 transfer 新情境应用题和一道 counterexample 边界或反例题，避免只抄原句的复述。"
    "每题生成不同 id、question、expected、rubric，并以原样短句引用 sources 的 source_id。"
    "参考答案必须有材料依据，材料范围外不可断言；出题不代表答题者已经知道参考答案。"
)

ANSWER_PROMPT = BOUNDARIES + (
    "只用 learned_notes 与你的理解独立回答 questions，逐一使用给出的 id。"
    "未提供参考答案和原始材料，不臆造测验成绩；不确定、缺少前提或需要查证要在答案中说明。"
)

GRADE_PROMPT = BOUNDARIES + (
    "逐一评阅 answers，严格对照每题 rubric、expected、来源引用与实际 sources。"
    "不因表达流畅判对；检查条件、推理、反例和新情境应用。"
    "缺少依据、题目本身有歧义或超出材料时 passed=false，gap 指明需要补查的问题。"
    "全部结论是模型自检，不是独立实验或真人考核；limitations 如实记录这种局限。"
)

def definition(doc):
    return hashlib.sha256(json.dumps({
        k: doc[k] for k in ("account_uid", "request", "request_quote", "objective", "criteria")
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def update(engine, goal, **changes):
    engine.role_run()
    data = {**deepcopy(goal["data"]), **changes, "updated_at": engine.clock().isoformat()}
    return engine.store.write("learning_goals", data, goal["version"], engine.identity(), data["id"])


def control(engine, goal_id, action, expected_version):
    engine.mode("role")
    store = engine.store
    with store.lock:
        goal = store.read("learning_goals", goal_id)
        doc = goal["data"]
        if doc["account_uid"] != store.read("manifest")["data"]["account_uid"]:
            raise AssistantError("goal_account", "目标不属于当前账号")
        if goal["version"] != expected_version:
            raise AssistantError("context_changed", "目标已被修改，请重读后操作")
        if action == "resume":
            if doc["status"] in ("completed", "cancelled") or doc["round"] > doc["round_limit"]:
                raise AssistantError("goal_finished", "目标已结束或轮数用完；需要明确新目标或修改本地上限")
            if any(r["data"]["id"] != goal_id and r["data"]["account_uid"] == doc["account_uid"]
                   and r["data"]["status"] == "active" for r in store.records("learning_goals")[0]):
                raise AssistantError("goal_active", "已有其他活动目标")
            if not same_versions(store, "conversation_turns", [doc["request"]]):
                raise AssistantError("context_changed", "用户原始要求已经变化，需重新确认目标")
        doc.update(status={"pause": "paused", "resume": "active", "cancel": "cancelled"}[action],
                   reason="user_" + action, updated_at=engine.clock().isoformat())
        # Changing a goal never starts the assistant or launches a daemon.
        return store.write("learning_goals", doc, expected_version, engine.identity(), goal_id)


def status(engine, goal_id=None):
    engine.mode()
    store = engine.store
    account = store.read("manifest")["data"]["account_uid"]
    goals = [r for r in store.records("learning_goals")[0] if r["data"]["account_uid"] == account]
    goals.sort(key=lambda r: (r["data"]["status"] == "active", recorded_time(r["data"])), reverse=True)
    if goal_id:
        goals = [r for r in goals if r["data"]["id"] == goal_id]
        if not goals:
            raise AssistantError("not_found", "当前账号没有这个目标")
    for goal in goals:
        goal["request_current"] = same_versions(store, "conversation_turns", [goal["data"]["request"]])
        goal["knowledge_current"] = same_versions(store, "knowledge", goal["data"]["knowledge_refs"])
        goal["disputed_knowledge"] = disputed_knowledge(store, account, goal["data"]["knowledge_refs"])
        goal["completion_current"] = False
        if goal["data"]["status"] == "completed":
            try:
                check = store.read("learning_checks", check_id(goal))
                goal["completion_current"] = (
                    goal["request_current"] and not goal["disputed_knowledge"]
                    and check_valid(store, goal, check) and check["data"]["passed"] is True
                )
                from .learning_tree import assessment_current, nodes_for

                nodes = nodes_for(store, goal)
                if nodes:
                    goal["completion_current"] = goal["completion_current"] and all(
                        assessment_current(store, goal, n) and n["data"]["mastery"] == 1 for n in nodes)
            except AssistantError:
                pass
    return {
        "goals": goals[:20], "total": len(goals), "run": store.state("run"),
        "checks": [{**r, "current": check_valid(store, goals[0], r)}
                   for r in store.records("learning_checks")[0]
                   if goal_id and r["data"]["account_uid"] == account and r["data"]["goal_id"] == goal_id],
        "verification": "模型自检，包含迁移题与反例题；不等于独立实测或用户已掌握",
    }


def active(engine):
    account = engine.store.read("manifest")["data"]["account_uid"]
    goals = [r for r in engine.store.records("learning_goals")[0]
             if r["data"]["account_uid"] == account and r["data"]["status"] == "active"]
    if len(goals) > 1:
        raise AssistantError("multiple_goals", "本地文件中有多个活动目标，请保留一个")
    return goals[0] if goals else None


def context(engine, goal):
    doc = goal["data"]
    records = [engine.store.read("knowledge", r["id"]) for r in doc["knowledge_refs"]]
    budget = max(1000, engine.store.read("companion")["data"]["context_char_limit"] - 14000)
    per_note = max(100, budget // max(1, len(records)))
    for record in records:
        record["content_truncated"] = len(record["data"]["content"]) > per_note
        record["data"]["content"] = record["data"]["content"][:per_note]
    from .learning_tree import assessment_current, nodes_for

    nodes = [{"id": n["data"]["id"], "title": n["data"]["title"], "parent_id": n["data"]["parent_id"],
              "prerequisites": n["data"]["prerequisites"],
              "mastery": n["data"]["mastery"] if assessment_current(engine.store, goal, n) else None,
              "gaps": [m["gap"] for m in n["data"]["mistakes"] if not m["resolved"]][-6:]}
             for n in nodes_for(engine.store, goal)]
    return {"objective": doc["objective"], "criteria": doc["criteria"], "previous_gaps": doc["gaps"],
            "queries": doc["queries"], "learned_notes": records, "knowledge_tree": nodes}


def goal_versions(engine, goal):
    verify_versions(engine.store, [("learning_goals", goal["data"]["id"], goal["version"])])
    if not same_versions(engine.store, "conversation_turns", [goal["data"]["request"]]):
        raise AssistantError("context_changed", "用户学习要求已经修改")
    if not same_versions(engine.store, "knowledge", goal["data"]["knowledge_refs"]):
        raise AssistantError("context_changed", "目标的知识依据已经修改")


def check_valid(store, goal, check):
    doc = check["data"]
    return (
        doc["account_uid"] == goal["data"]["account_uid"] and doc["goal_id"] == goal["data"]["id"]
        and doc["definition"] == definition(goal["data"])
        and same_versions(store, "knowledge", doc["knowledge_refs"])
        and same_versions(store, "source_snapshots", doc["source_refs"])
    )


def check_id(goal):
    return stable_id("check", json.dumps([definition(goal["data"]), goal["data"]["round"],
                                        goal["data"]["knowledge_refs"]], sort_keys=True))


def checked_ids(items, expected):
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise AssistantError("learning_check_ids", "答案或评分必须完整对应每道题且不能重复")


def validate_questions(questions, criteria, sources):
    """Shared by platform learning and private explicit tasks."""
    from .knowledge import validate_quotes

    ids = [question["id"] for question in questions]
    if len(ids) != len(set(ids)) or {q["criterion"] for q in questions} != set(range(len(criteria))):
        raise AssistantError("learning_coverage", "测验须完整覆盖目标标准且题号不能重复")
    if not {"transfer", "counterexample"} <= {q["kind"] for q in questions}:
        raise AssistantError("learning_coverage", "测验缺少迁移题或反例题")
    for question in questions:
        validate_quotes(question["evidence"], sources)
