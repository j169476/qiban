"""Explicit learning stages reuse the core's withheld-answer assessment contracts."""
from . import learning
from .errors import AssistantError


async def step(arguments, previous, generate, research):
    phase = previous["phase"] if previous else "start"
    data = previous["payload"] if previous else {"round": 1, "gaps": []}
    common = {"objective": arguments["objective"], "criteria": arguments["criteria"]}
    if phase in {"start", "research_again"}:
        sources = await research({**common, "previous_gaps": data.get("gaps", [])})
        return {"phase": "researched", "payload": {"round": data["round"], "sources": sources},
                "summary": "已取得材料，接下来检查理解与适用边界。", "complete": False}
    if phase == "researched":
        value = await generate(learning.QUIZ_PROMPT, {**common, "sources": data["sources"]}, learning.QUIZ_SCHEMA)
        learning.validate_questions(value["questions"], arguments["criteria"], {s["id"]: s for s in data["sources"]})
        return {"phase": "questions_ready", "payload": {**data, **value}, "complete": False}
    if phase == "questions_ready":
        # Neither expected answers, rubrics nor original excerpts reach this call.
        value = await generate(learning.ANSWER_PROMPT, {
            "questions": [{"id": q["id"], "question": q["question"]} for q in data["questions"]],
            "learned_notes": [s["note"] for s in data["sources"]]}, learning.ANSWER_SCHEMA)
        learning.checked_ids(value["answers"], [q["id"] for q in data["questions"]])
        return {"phase": "answered", "payload": {**data, **value}, "complete": False}
    if phase != "answered":
        raise AssistantError("task_phase", "学习阶段无效，需重新检查任务")
    value = await generate(learning.GRADE_PROMPT, {"questions": data["questions"], "answers": data["answers"],
                                                 "sources": data["sources"]}, learning.GRADE_SCHEMA)
    learning.checked_ids(value["grades"], [q["id"] for q in data["questions"]])
    passed = all(grade["passed"] for grade in value["grades"])
    final = passed or data["round"] >= arguments["round_limit"]
    gaps = [grade["gap"] or grade["reason"] for grade in value["grades"] if not grade["passed"]]
    return {"phase": "assessed" if final else "research_again", "complete": final,
            "payload": {**data, **value, "passed": passed, "gaps": gaps,
                        "round": data["round"] if final else data["round"] + 1},
            "summary": ("模型自检通过；这不是独立实测或用户掌握证明。" if passed else
                        "本轮仍有知识缺口：" + "；".join(gaps))[:12000]}
