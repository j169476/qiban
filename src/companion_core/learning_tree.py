"""Criterion trees and evidence-based, resumable spaced self-checks."""

from copy import deepcopy
from datetime import datetime, timedelta

from .cognition import stable_id, verify_versions
from .errors import AssistantError
from .knowledge import disputed_knowledge, ref, same_versions
from .schema import obj


def nodes_for(store, goal):
    from .learning import definition

    return sorted([n for n in store.records("learning_nodes")[0]
                   if n["data"]["goal_id"] == goal["data"]["id"]
                   and n["data"]["account_uid"] == goal["data"]["account_uid"]
                   and n["data"]["definition"] == definition(goal["data"])],
                  key=lambda n: n["data"]["criterion"])


def ensure(engine, goal):
    from .learning import definition

    doc, store = goal["data"], engine.store
    signature = definition(doc)
    with store.lock:
        existing = {n["data"]["criterion"] for n in nodes_for(store, goal)}
        for i, title in enumerate(doc["criteria"]):
            if i in existing:
                continue
            item_id = stable_id("node", f"{doc['id']}:{signature}:{i}")
            store.write("learning_nodes", {
                "schema_version": 1, "id": item_id, "account_uid": doc["account_uid"],
                "goal_id": doc["id"], "definition": signature, "criterion": i, "title": title,
                "parent_id": None, "prerequisites": [], "mastery": None, "streak": 0,
                "next_review_at": None, "assessment": None, "mistakes": [],
                "source": {"type": "reflection", "recorded_at": engine.clock().isoformat(),
                           "reference": doc["id"]}, "locked": False,
            }, None, engine.identity(), item_id)
    return nodes_for(store, goal)


def graph(nodes):
    """Reject missing links, self-links and cycles, including combinations of both edge types."""
    data = {n["data"]["id"]: n["data"] for n in nodes}
    visiting, visited = set(), set()

    def visit(key):
        if key not in data or key in visiting:
            raise AssistantError("learning_graph", "知识树不能包含跨目标、缺失或成环的依赖")
        if key in visited:
            return
        visiting.add(key)
        node = data[key]
        for other in [*node["prerequisites"], *([node["parent_id"]] if node["parent_id"] else [])]:
            visit(other)
        visiting.remove(key)
        visited.add(key)
    for key in data:
        visit(key)


def assessment_current(store, goal, node):
    from .learning import check_valid, definition

    doc, target = node["data"], node["data"]["assessment"]
    if (not target or doc["definition"] != definition(goal["data"])
            or doc["criterion"] >= len(goal["data"]["criteria"])
            or doc["title"] != goal["data"]["criteria"][doc["criterion"]]
            or not same_versions(store, "conversation_turns", [goal["data"]["request"]])):
        return False
    if disputed_knowledge(store, goal["data"]["account_uid"], goal["data"]["knowledge_refs"]):
        return False
    try:
        record = store.read(target["kind"], target["id"])
        if record["version"] != target["version"] or record["data"]["status"] != "graded":
            return False
        if target["kind"] == "learning_checks":
            check = record
        else:
            base = record["data"]["base_check"]
            check = store.read("learning_checks", base["id"])
            if check["version"] != base["version"]:
                return False
        questions = [q for q in check["data"]["questions"] if q["criterion"] == doc["criterion"]]
        grades = {g["id"]: g for g in record["data"]["grades"]}
        if not questions or any(q["id"] not in grades for q in questions):
            return False
        score = sum(grades[q["id"]]["passed"] for q in questions) / len(questions)
        return check_valid(store, goal, check) and doc["mastery"] == score
    except AssistantError:
        return False


def status(engine, goal_id):
    from .learning import status as goals

    goal = goals(engine, goal_id)["goals"][0]
    nodes = nodes_for(engine.store, goal)
    try:
        graph(nodes)
        graph_error = None
    except AssistantError as exc:
        graph_error = exc.as_dict()
    now = engine.clock()
    for node in nodes:
        current = assessment_current(engine.store, goal, node)
        node["evidence_current"] = current
        node["due"] = bool(current and node["data"]["next_review_at"]
                           and datetime.fromisoformat(node["data"]["next_review_at"]) <= now)
    return {"goal": goal, "nodes": nodes, "graph_error": graph_error,
            "verification": "各节点对应目标验收标准；掌握度是模型自检正确比例，不代表用户掌握或独立实测"}


def configure(engine, node_id, expected_version, parent_id, prerequisites):
    engine.mode("role")
    store = engine.store
    with store.lock:
        node = store.read("learning_nodes", node_id)
        goal = store.read("learning_goals", node["data"]["goal_id"])
        if node["data"]["account_uid"] != store.read("manifest")["data"]["account_uid"]:
            raise AssistantError("goal_account", "知识点不属于当前账号")
        proposed = {**node, "data": {**node["data"], "parent_id": parent_id, "prerequisites": prerequisites}}
        nodes = nodes_for(store, goal)
        if node_id not in {n["data"]["id"] for n in nodes}:
            raise AssistantError("context_changed", "目标已改变，请使用当前知识树")
        graph([proposed if n["data"]["id"] == node_id else n for n in nodes])
        return store.write("learning_nodes", proposed["data"], expected_version, engine.identity(), node_id)


def apply_assessment(engine, goal, check, assessment, kind="learning_checks"):
    """Idempotent across restarts; grades, answers and mistakes retain their exact evidence."""
    from .learning import check_valid

    store = engine.store
    with store.lock:
        if not check_valid(store, goal, check):
            raise AssistantError("context_changed", "掌握度的测验依据已变化")
        target = {"kind": kind, **ref(assessment)}
        grades = {g["id"]: g for g in assessment["data"]["grades"]}
        answers = {a["id"]: a["answer"] for a in assessment["data"]["answers"]}
        for node in ensure(engine, goal):
            doc = deepcopy(node["data"])
            if (doc["locked"] or doc["assessment"] == target
                    or doc["title"] != goal["data"]["criteria"][doc["criterion"]]):
                continue
            questions = [q for q in check["data"]["questions"] if q["criterion"] == doc["criterion"]]
            if not questions or any(q["id"] not in grades for q in questions):
                continue
            score = sum(grades[q["id"]]["passed"] for q in questions) / len(questions)
            prior_current = assessment_current(store, goal, node)
            streak = min(1000, (doc["streak"] if prior_current else 0) + 1) if score == 1 else 0
            interval = [1, 3, 7, 14, 30][min(streak, 4)] if score == 1 else 1
            mistakes = doc["mistakes"]
            if score == 1:
                mistakes = [{**m, "resolved": True} for m in mistakes]
            for q in questions:
                grade = grades[q["id"]]
                if not grade["passed"]:
                    mistakes.append({"assessment": target, "question_id": q["id"], "question": q["question"],
                                     "answer": answers.get(q["id"], ""), "gap": grade["gap"] or grade["reason"],
                                     "resolved": False})
            doc.update(mastery=score, streak=streak, assessment=target, mistakes=mistakes[-30:],
                       next_review_at=(engine.clock() + timedelta(days=interval)).isoformat())
            store.write("learning_nodes", doc, node["version"], engine.identity(), doc["id"])


async def review_step(scheduler):
    """Only explicit completed goals, during an already-running role, one model call per cycle."""
    from .learning import (
        ANSWER_PROMPT,
        ANSWER_SCHEMA,
        GRADE_PROMPT,
        GRADE_SCHEMA,
        check_valid,
        checked_ids,
        context,
    )

    engine, store = scheduler.engine, scheduler.engine.store
    engine.role_run()
    settings = store.read("companion")
    if not settings["data"].get("spaced_reviews_enabled", True):
        return None
    account = store.read("manifest")["data"]["account_uid"]
    for goal in store.records("learning_goals")[0]:
        if goal["data"]["account_uid"] != account or goal["data"]["status"] != "completed" or goal["data"]["locked"]:
            continue
        nodes = nodes_for(store, goal)
        try:
            graph(nodes)
        except AssistantError:
            continue
        due = [n for n in nodes if not n["data"]["locked"] and assessment_current(store, goal, n)
               and n["data"]["next_review_at"] and datetime.fromisoformat(n["data"]["next_review_at"]) <= engine.clock()]
        if not due:
            continue
        # One whole-goal review preserves transfer/counterexample coverage across linked nodes.
        due.sort(key=lambda n: n["data"]["next_review_at"])
        key = stable_id("review", goal["data"]["id"] + due[0]["data"]["next_review_at"])
        try:
            review = store.read("learning_reviews", key)
        except AssistantError as exc:
            if exc.code != "not_found":
                raise
            target = due[0]["data"]["assessment"]
            base = store.read(target["kind"], target["id"])
            if target["kind"] == "learning_reviews":
                base = store.read("learning_checks", base["data"]["base_check"]["id"])
            review = store.write("learning_reviews", {
                "schema_version": 1, "id": key, "account_uid": account, "goal_id": goal["data"]["id"],
                "base_check": ref(base), "node_refs": [ref(n) for n in nodes], "answers": [], "grades": [],
                "status": "questions_ready", "limitations": "沿用已存题目的间隔模型自检，不是独立考核",
                "source": {"type": "reflection", "recorded_at": engine.clock().isoformat(), "reference": goal["data"]["id"]},
                "locked": False,
            }, None, engine.identity(), key)
        check = store.read("learning_checks", review["data"]["base_check"]["id"])
        if review["data"]["locked"] or check["version"] != review["data"]["base_check"]["version"]:
            continue
        if not check_valid(store, goal, check) or not same_versions(store, "conversation_turns", [goal["data"]["request"]]):
            continue
        versions = [("learning_goals", goal["data"]["id"], goal["version"]),
                    ("learning_checks", check["data"]["id"], check["version"]),
                    ("companion", None, settings["version"])]

        def verify():
            scheduler.alive()
            verify_versions(store, versions)
            if not check_valid(store, goal, check) or not same_versions(store, "conversation_turns", [goal["data"]["request"]]):
                raise AssistantError("context_changed", "复习期间目标或学习依据已变化")
            if disputed_knowledge(store, account, goal["data"]["knowledge_refs"]):
                raise AssistantError("context_changed", "复习期间知识出现未决争议，需要先复核材料")

        data = deepcopy(review["data"])
        if data["status"] != "graded" and not scheduler.remaining_model_calls():
            return {"type": "review", "id": key, "waiting": "model_quota"}
        if data["status"] == "questions_ready":
            result = await scheduler.model(ANSWER_PROMPT, {
                "questions": [{"id": q["id"], "question": q["question"]} for q in check["data"]["questions"]],
                "learned_notes": context(engine, goal)["learned_notes"],
            }, ANSWER_SCHEMA)
            verify()
            checked_ids(result["answers"], [q["id"] for q in check["data"]["questions"]])
            data.update(answers=result["answers"], status="answered")
        elif data["status"] == "answered":
            sources = [store.read("source_snapshots", r["id"])["data"] for r in check["data"]["source_refs"]]
            # Bound original material to quoted segments, just as the initial grading phase.
            quotes = [e for q in check["data"]["questions"] for e in q["evidence"]]
            excerpts = [{"id": s["id"], "coverage": s["coverage"], "segments": [seg for seg in s["segments"]
                         if any(e["source_id"] == s["id"] and e["quote"] in seg["text"] for e in quotes)]}
                        for s in sources]
            result = await scheduler.model(GRADE_PROMPT, {"questions": check["data"]["questions"],
                                           "answers": data["answers"], "sources": excerpts}, GRADE_SCHEMA)
            verify()
            checked_ids(result["grades"], [q["id"] for q in check["data"]["questions"]])
            data.update(grades=result["grades"], status="graded", limitations=result["limitations"])
        if data != review["data"]:
            review = store.write("learning_reviews", data, review["version"], engine.identity(), key)
        if data["status"] == "graded":
            verify()
            apply_assessment(engine, goal, check, review, "learning_reviews")
        return {"type": "review", "id": key, "phase": data["status"]}
    return None


NODE_CONFIG = obj({"node_id": {"type": "string"}, "expected_version": {"type": "string"},
                   "parent_id": {"type": ["string", "null"]},
                   "prerequisites": {"type": "array", "items": {"type": "string"}, "maxItems": 5}})
