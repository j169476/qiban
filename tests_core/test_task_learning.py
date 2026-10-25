from copy import deepcopy

import pytest

from companion_core import task_learning
from companion_core.errors import AssistantError

SOURCES = [{"id": "source-one", "segments": [{"text": "material with conditions"}], "note": "learned note"}]
QUESTIONS = [{"id": str(i), "criterion": 0, "kind": kind, "question": "question " + str(i),
              "expected": "withheld-expected", "rubric": "withheld-rubric",
              "evidence": [{"source_id": "source-one", "quote": "with conditions"}]}
             for i, kind in enumerate(("transfer", "counterexample"))]


@pytest.mark.asyncio
@pytest.mark.parametrize("passed", [True, False])
async def test_explicit_learning_reuses_blind_assessment_and_bounded_gap_research(passed):
    researches, answers = [], []

    async def research(context):
        researches.append(context)
        return deepcopy(SOURCES)

    async def generate(prompt, context, schema):
        if "questions" in schema["properties"]:
            return {"questions": deepcopy(QUESTIONS)}
        if "answers" in schema["properties"]:
            answers.append(context)
            assert set(context) == {"questions", "learned_notes"}
            assert all(set(question) == {"id", "question"} for question in context["questions"])
            assert "withheld-" not in str(context) and "material with conditions" not in str(context)
            return {"answers": [{"id": str(i), "answer": "my answer"} for i in range(2)]}
        return {"grades": [{"id": str(i), "passed": passed, "gap": "check boundary" if not passed else "",
                            "reason": "synthetic"} for i in range(2)], "limitations": "model only"}

    arguments = {"objective": "learn a concept", "criteria": ["apply with conditions"], "round_limit": 2}
    result = None
    for _ in range(8):
        result = await task_learning.step(arguments, result, generate, research)
        if result["complete"]:
            break
    assert result["complete"] and result["payload"]["passed"] == passed
    assert len(researches) == len(answers) == (1 if passed else 2)
    if not passed:
        assert researches[1]["previous_gaps"] == ["check boundary", "check boundary"]


@pytest.mark.asyncio
async def test_learning_cannot_accept_fabricated_material_citations():
    async def generate(*args):
        questions = deepcopy(QUESTIONS)
        questions[0]["evidence"][0]["quote"] = "not in the material"
        return {"questions": questions}

    with pytest.raises(AssistantError):
        await task_learning.step({"objective": "x", "criteria": ["x"], "round_limit": 1},
            {"phase": "researched", "payload": {"round": 1, "sources": SOURCES}}, generate, None)
