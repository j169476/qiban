"""Explicit user-requested learning, with resumable reading and withheld-answer checks."""

from __future__ import annotations

import json
import re
from copy import deepcopy

from companion_core.knowledge import disputed_knowledge
from companion_core.learning import active as active
from companion_core.learning import check_id as check_id
from companion_core.learning import check_valid as check_valid
from companion_core.learning import checked_ids as checked_ids
from companion_core.learning import context as context
from companion_core.learning import control as control
from companion_core.learning import definition as definition
from companion_core.learning import goal_versions as goal_versions
from companion_core.learning import status as status
from companion_core.learning import update as update

from .cognition import BOUNDARIES, save_learning, stable_id, text, texts, verify_versions
from .errors import AssistantError
from .knowledge import (
    ref,
    same_versions,
    source_excerpt,
    source_for,
    user_evidence,
)
from .schema import ANSWER, GRADE, QUESTION, VIDEO_WINDOW, obj

PLAN_SCHEMA = obj({"gaps": texts(8, 1000), "query": {"type": "string", "minLength": 1, "maxLength": 100}})
SELECT_SCHEMA = obj({"bvid": text(12), "reason": text(1500)})
READ_SCHEMA = obj({"knowledge": text(12000), "tags": texts(8, 100), "video_topics": texts(6, 100),
                   "video_gap": {"anyOf": [{"type": "null"}, VIDEO_WINDOW]}})
QUIZ_SCHEMA = obj({"questions": {"type": "array", "items": QUESTION, "minItems": 2, "maxItems": 8}})
ANSWER_SCHEMA = obj({"answers": {"type": "array", "items": ANSWER, "minItems": 2, "maxItems": 8}})
GRADE_SCHEMA = obj({
    "grades": {"type": "array", "items": GRADE, "minItems": 2, "maxItems": 8},
    "limitations": text(2000),
})
PLAN_PROMPT = BOUNDARIES + (
    "这是用户明确要求的有限学习任务。仅围绕 objective 与 criteria 梳理 gaps 并生成一条新搜索词。"
    "吸收 previous_gaps 与错题反馈，不重复 queries，不增加无关长期目标；没有证据就写明缺口。"
)
SELECT_PROMPT = BOUNDARIES + (
    "针对用户的 objective、criteria 和 gaps，从 candidates 选择最有帮助且能补充现有来源的一条。"
    "不能编造候选 BV；不适合就 bvid 为空。标题只用于选择，不能当作已学会。"
)
READ_PROMPT = BOUNDARIES + (
    "根据用户目标阅读这次 video，提炼可追溯知识，写清适用条件、例外和未覆盖部分。"
    "只把字幕或实际附带的画面作为材料；标题简介或封面不能成为知识。"
    "不生成外部互动动作，不将看到资料描述成已通过测试；材料不够时 knowledge 为空。"
    "如目标的具体缺口需要同一视频的未读区间，用 video_gap 指定时间范围、text/visual 和理由，"
    "否则为 null；不要求把所有视频都读完，不重复已覆盖区间。"
)
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




def create(engine, objective, criteria, source_turn_id, request_quote):
    request = user_evidence(engine, source_turn_id, request_quote)
    store = engine.store
    account = store.read("manifest")["data"]["account_uid"]
    item_id = stable_id("goal", json.dumps([account, ref(request)], sort_keys=True))
    with store.lock:
        try:
            return {"duplicate": True, **store.read("learning_goals", item_id)}
        except AssistantError as exc:
            if exc.code != "not_found":
                raise
        if any(r["data"]["account_uid"] == account and r["data"]["status"] == "active"
               for r in store.records("learning_goals")[0]):
            raise AssistantError("goal_active", "已有一个活动学习目标，请先暂停或结束它")
        known = [ref(r) for r in store.search(objective, limit=20) if r["kind"] == "knowledge"][:6]
        now = engine.clock().isoformat()
        goal = store.write("learning_goals", {
            "schema_version": 1, "id": item_id, "account_uid": account,
            "request": ref(request), "request_quote": request_quote, "objective": objective,
            "criteria": criteria, "status": "active", "phase": "plan", "reason": "explicit_user_request",
            "gaps": [], "round": 1,
            "round_limit": store.read("companion")["data"].get("learning_round_limit", 6),
            "knowledge_refs": known, "visited_bvids": [], "queries": [], "query": "", "selected_bvid": "",
            "check_ids": [], "updated_at": now,
            "source": {"type": "user_instruction", "recorded_at": now, "reference": source_turn_id},
            "locked": False,
        }, None, engine.identity(), item_id)
        from .learning_tree import ensure

        ensure(engine, goal)
        return goal










def exhausted(engine, goal, reason, gaps=()):
    doc = goal["data"]
    next_round = doc["round"] + 1
    return update(engine, goal, round=next_round, phase="plan", selected_bvid="", query="",
                  status="paused" if next_round > doc["round_limit"] else "active",
                  reason="round_limit: " + reason if next_round > doc["round_limit"] else reason,
                  gaps=list(gaps or [reason])[:8])












async def step(scheduler):
    """At most one stage per cycle; no goal creation or platform posting in this worker."""
    engine, store = scheduler.engine, scheduler.engine.store
    engine.role_run()
    goal = active(engine)
    if not goal:
        from .learning_tree import review_step

        return await review_step(scheduler)
    from .learning_tree import ensure

    ensure(engine, goal)
    doc = goal["data"]
    if doc["locked"]:
        return {"type": "goal", "id": doc["id"], "waiting": "locked"}
    if not same_versions(store, "conversation_turns", [doc["request"]]):
        update(engine, goal, status="paused", reason="request_changed")
        return {"type": "goal", "id": doc["id"], "waiting": "request_changed"}
    if doc["reason"] == "native_web_pending":
        jobs = [r for r in store.records("native_jobs")[0]
                if r["data"]["goal"] == ref(goal) and r["data"]["status"] in ("pending", "running")]
        if jobs:
            return {"type": "goal", "id": doc["id"], "waiting": "native_web"}
        exhausted(engine, goal, "原生网页补查没有形成可用材料")
        return {"type": "goal", "id": doc["id"], "waiting": "native_web_failed"}
    if any(window.get("goal") == ref(goal) and window["status"] in ("pending", "reading")
           for record in store.records("video_progress")[0] for window in record["data"]["requests"]):
        return {"type": "goal", "id": doc["id"], "waiting": "video_gap"}
    if doc["round"] > doc["round_limit"]:
        update(engine, goal, status="paused", reason="round_limit")
        return {"type": "goal", "id": doc["id"], "waiting": "round_limit"}
    if not same_versions(store, "knowledge", doc["knowledge_refs"]):
        # Keep old evidence/checks on disk, but explicitly replan using current note versions.
        refs = []
        for item in doc["knowledge_refs"]:
            try:
                refs.append(ref(store.read("knowledge", item["id"])))
            except AssistantError:
                continue
        update(engine, goal, knowledge_refs=refs, phase="plan", reason="knowledge_changed",
               gaps=["先复核人工修改后的知识与旧测验结论"])
        return {"type": "goal", "id": doc["id"], "phase": "replan"}
    if not scheduler.remaining_model_calls():
        return {"type": "goal", "id": doc["id"], "waiting": "model_quota"}
    settings = store.read("companion")
    preferences = store.read("preferences")
    config_versions = [("companion", None, settings["version"]), ("preferences", None, preferences["version"])]
    blocked = preferences["data"]
    phase = doc["phase"]

    def verify():
        scheduler.alive()
        goal_versions(engine, goal)
        verify_versions(store, config_versions)

    if phase == "plan":
        result = await scheduler.model(PLAN_PROMPT, context(engine, goal), PLAN_SCHEMA)
        verify()
        if result["query"].casefold() in {q.casefold() for q in doc["queries"]}:
            exhausted(engine, goal, "搜索词重复，需要换一条补查路径", result["gaps"])
        else:
            update(engine, goal, gaps=result["gaps"], query=result["query"], phase="search", reason="planned")
    elif phase == "search":
        from .discovery import search, search_usage

        if not search_usage(engine)["remaining"]:
            return {"type": "goal", "id": doc["id"], "waiting": "search_quota"}
        candidates = await search(engine, doc["query"], limit=settings["data"]["candidate_limit"])
        verify()
        candidates = [
            c for c in candidates if c["bvid"] not in doc["visited_bvids"]
            and re.fullmatch(r"BV[A-Za-z0-9]{10}", c["bvid"]) and c["owner_uid"] not in blocked["blocked_uids"]
            and not any(t.casefold() in (c["title"] + " " + c.get("description", "")).casefold()
                        for t in blocked["blocked_topics"] if t)
        ]
        if not candidates:
            goal = update(engine, goal, queries=[*doc["queries"], doc["query"]])
            if settings["data"].get("native_tools_enabled", True):
                from .native_tools import create as create_native

                goal = update(engine, goal, reason="native_web_pending")
                try:
                    create_native(engine, "web", doc["query"], goal_id=doc["id"])
                except AssistantError:
                    exhausted(engine, goal, "本轮没有新的候选，网页补查不可用")
            else:
                exhausted(engine, goal, "本轮没有新的可用候选")
        else:
            from .algorithm_inputs import select

            choice, selection_versions = select(engine, candidates, query=doc["query"], seed=doc["id"])
            selectable = [c for c in candidates if c["bvid"] in choice["eligible_ids"]]
            if choice["route"] == "model":
                result = await scheduler.model(SELECT_PROMPT, {**context(engine, goal), "candidates": selectable},
                                               SELECT_SCHEMA)
            else:
                result = {"bvid": choice["selected"], "reason": choice["reason"]}
            verify()
            verify_versions(store, selection_versions)
            if result["bvid"] and result["bvid"] not in {c["bvid"] for c in selectable}:
                raise AssistantError("ungrounded_target", "目标学习选片不在本轮搜索结果中")
            goal = update(engine, goal, queries=[*doc["queries"], doc["query"]],
                          selected_bvid=result["bvid"], reason=result["reason"])
            if result["bvid"]:
                update(engine, goal, phase="read")
            else:
                exhausted(engine, goal, "搜索结果不能补足当前缺口")
    elif phase == "read":
        from .discovery import observe
        from .media import validated_image_paths

        try:
            video = await engine.video_context(doc["selected_bvid"])
        except AssistantError as exc:
            if exc.code == "video_quota":
                return {"type": "goal", "id": doc["id"], "waiting": "video_quota"}
            raise
        verify()
        if video.get("owner_uid") in blocked["blocked_uids"] or any(
            t.casefold() in (video["title"] + " " + video.get("description", "")).casefold()
            for t in blocked["blocked_topics"] if t
        ) or video["coverage"]["kind"] == "metadata_only":
            goal = update(engine, goal, visited_bvids=[*doc["visited_bvids"], video["bvid"]])
            exhausted(engine, goal, "候选无实际材料或属于排除项")
        else:
            result = await scheduler.model(READ_PROMPT, {**context(engine, goal), "video": video},
                                           READ_SCHEMA, validated_image_paths(store, video.get("images", [])))
            verify()
            if not result["knowledge"].strip():
                goal = update(engine, goal, visited_bvids=[*doc["visited_bvids"], video["bvid"]])
                if result.get("video_gap"):
                    from .video_progress import from_decision

                    queued = from_decision(engine, {"video": video, "learning_goal": doc["id"]}, result)
                    if not queued or not queued.get("progress", {}).get("pending"):
                        exhausted(engine, goal, "实际材料不足，补读没有产生待办")
                else:
                    exhausted(engine, goal, "实际材料不足以形成目标知识")
            else:
                saved = save_learning(engine, {"video": video, "events": [], "learning_goal": doc["id"]}, {
                    **result, "note": "", "memory": "", "skill": None, "emotion": None,
                    "interest_signals": [], "relationships": [], "action": None,
                })
                knowledge = store.read("knowledge", saved["knowledge"])
                refs = [r for r in doc["knowledge_refs"] if r["id"] != saved["knowledge"]]
                update(engine, goal, knowledge_refs=[*refs, ref(knowledge)][-20:],
                       visited_bvids=[*doc["visited_bvids"], video["bvid"]], phase="quiz", reason="material_read")
                observe(engine, video, "read", "search", result["video_topics"])
                if result.get("video_gap"):
                    from .video_progress import from_decision

                    from_decision(engine, {"video": video, "learning_goal": doc["id"]}, result)
    elif phase in ("quiz", "answer", "grade"):
        await check_step(scheduler, goal, verify)
    else:
        update(engine, goal, status="paused", reason="invalid_active_phase")
    store.audit("learning", {"event": "goal_step", "goal_id": doc["id"], "phase": phase, "round": doc["round"]})
    return {"type": "goal", "id": doc["id"], "phase": phase}


async def check_step(scheduler, goal, verify):
    engine, store = scheduler.engine, scheduler.engine.store
    doc = goal["data"]
    item_id = check_id(goal)
    try:
        check = store.read("learning_checks", item_id)
    except AssistantError as exc:
        if exc.code != "not_found":
            raise
        check = None
    if check and not check_valid(store, goal, check):
        exhausted(engine, goal, "测验依据已变化，需要重新阅读和验证")
        return
    if not check:
        sources = {}
        for note in context(engine, goal)["learned_notes"]:
            source = source_for(store, note, doc["account_uid"])
            if source and source["data"]["segments"]:
                sources[source["data"]["id"]] = source
        if not sources:
            exhausted(engine, goal, "没有可引用的文字材料，暂不能建立可靠题目与评分依据")
            return
        size = max(500, (store.read("companion")["data"]["context_char_limit"] - 12000) // len(sources))
        excerpts = [source_excerpt(s, doc["objective"], size) for s in sources.values()]
        result = await scheduler.model(QUIZ_PROMPT, {
            "objective": doc["objective"], "criteria": doc["criteria"], "sources": excerpts,
        }, QUIZ_SCHEMA)
        verify()
        verify_versions(store, [("source_snapshots", s["data"]["id"], s["version"]) for s in sources.values()])
        questions = result["questions"]
        from companion_core.learning import validate_questions

        validate_questions(questions, doc["criteria"], {s["id"]: s for s in excerpts})
        check = store.write("learning_checks", {
            "schema_version": 1, "id": item_id, "account_uid": doc["account_uid"],
            "goal_id": doc["id"], "definition": definition(doc), "round": doc["round"],
            "knowledge_refs": doc["knowledge_refs"], "source_refs": [ref(s) for s in sources.values()],
            "questions": questions, "answers": [], "grades": [], "status": "questions_ready", "passed": None,
            "limitations": "待进行模型自检；题目和评分均非独立实测",
            "source": {"type": "reflection", "recorded_at": engine.clock().isoformat(), "reference": doc["id"]},
            "locked": False,
        }, None, engine.identity(), item_id)
        update(engine, goal, phase="answer", check_ids=list(dict.fromkeys([*doc["check_ids"], item_id])))
        return
    data = deepcopy(check["data"])
    if data["status"] == "questions_ready":
        # Deliberately omit expected/rubric/evidence and original sources from the answering call.
        result = await scheduler.model(ANSWER_PROMPT, {
            "questions": [{"id": q["id"], "question": q["question"]} for q in data["questions"]],
            "learned_notes": context(engine, goal)["learned_notes"],
        }, ANSWER_SCHEMA)
        verify()
        if not check_valid(store, goal, check):
            raise AssistantError("context_changed", "作答期间测验依据已变化")
        checked_ids(result["answers"], [q["id"] for q in data["questions"]])
        data.update(answers=result["answers"], status="answered")
        store.write("learning_checks", data, check["version"], engine.identity(), item_id)
        update(engine, goal, phase="grade", check_ids=list(dict.fromkeys([*doc["check_ids"], item_id])))
        return
    if data["status"] == "answered":
        sources = [store.read("source_snapshots", r["id"]) for r in data["source_refs"]]
        # Quotes and their complete containing segments are the relevant grading evidence.
        citations = [e for q in data["questions"] for e in q["evidence"]]
        excerpts = [{
            "id": s["data"]["id"], "coverage": s["data"]["coverage"],
            "segments": [seg for seg in s["data"]["segments"] if any(
                e["source_id"] == s["data"]["id"] and e["quote"] in seg["text"] for e in citations
            )],
        } for s in sources]
        result = await scheduler.model(GRADE_PROMPT, {
            "questions": data["questions"], "answers": data["answers"], "sources": excerpts,
        }, GRADE_SCHEMA)
        verify()
        if not check_valid(store, goal, check):
            raise AssistantError("context_changed", "评阅期间测验依据已变化")
        checked_ids(result["grades"], [q["id"] for q in data["questions"]])
        passed = all(g["passed"] for g in result["grades"])
        data.update(grades=result["grades"], limitations=result["limitations"], passed=passed, status="graded")
        check = store.write("learning_checks", data, check["version"], engine.identity(), item_id)
    verify()
    from .learning_tree import apply_assessment

    apply_assessment(engine, goal, check, check)
    disputed = disputed_knowledge(store, doc["account_uid"], doc["knowledge_refs"])
    if check["data"]["passed"] and not disputed:
        update(engine, goal, phase="done", status="completed", reason="model_check_passed", gaps=[])
    else:
        gaps = [g["gap"] or g["reason"] for g in check["data"]["grades"] if not g["passed"]]
        gaps += [f"知识 {item_id} 仍有未决复查问题" for item_id in disputed]
        exhausted(engine, goal, "模型自检未通过或知识仍有争议", gaps)
