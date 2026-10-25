import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("answer_evaluation", ROOT / "scripts/evaluate_answer_pipeline.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
CASES = json.loads((ROOT / "tests_core/fixtures/answer_pipeline.json").read_text(encoding="utf-8"))["cases"]


class ScriptedReader:
    """A plumbing fixture, deliberately not a measurement of a real model's reasoning."""
    def __init__(self):
        self.contexts = []

    async def generate(self, instructions, context, schema, images=(), **options):
        options["guard"]()
        self.contexts.append(deepcopy(context))
        assert "expected" not in context and "evidence_ids" not in context.get("event", {})
        if "queries" in schema["properties"]:
            return {"queries": ["备用钥匙", "住城市", "甜咖啡", "猫名字"]}
        text = context["event"]["text"]
        result = {"note": "", "action": None, "memories": [], "identity_proposals": [],
                  "relationship": None, "fact_updates": [], "growth_update": None}
        facts = context.get("personal_facts", {}).get("items", [])
        if context["event"]["native_id"] != "question":
            value = next((city for city in ("秘密城", "杭州", "上海") if city in text), None)
            if value and "住" in text or "搬到杭州" in text:
                value = value or "杭州"
                quote = "现在住杭州" if "以前" in text else text
                result["fact_updates"] = [{"attribute": "residence", "value": value, "quote": quote,
                    "valid_from": "2026-12-01" if "2026-12-01" in text else None,
                    "supersedes": [row["id"] for row in facts if row["attribute"] == "residence"
                                   and row["status"] in {"active", "disputed"}] if "现在" in text or "搬到" in text else []}]
            return result
        sources = [*context["conversation"]["turns"], *context["conversation"]["recalled"]]
        answer, citations, abstain = "不知道，当前材料没有提供。", [], True
        if "冲突" in text:
            conflicts = [row for row in facts if row["status"] == "disputed"]
            if conflicts:
                answer, citations = "住址存在冲突，不能确定。", [row["id"] for row in conflicts]
        elif "城市" in text:
            active = [row for row in facts if row["attribute"] == "residence" and row["status"] == "active"]
            if active:
                answer, citations, abstain = active[0]["value"], [active[0]["id"]], False
        elif "甜咖啡" in text:
            negative = next((row for row in sources if "我不喜欢甜咖啡" in row["data"]["content"]), None)
            if negative:
                answer, citations, abstain = "不喜欢甜咖啡。", [negative["data"]["id"]], False
        else:
            key = next((row for row in sources if "备用钥匙放在灰色盒子第二层" in row["data"]["content"]), None)
            if key:
                answer, citations, abstain = "灰色盒子第二层。", [key["data"]["id"]], False
        result["action"] = {"capability": "message.reply", "arguments": {"text": answer, "evidence_ids": citations, "abstain": abstain}}
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
async def test_full_pipeline_actual_and_oracle_preserve_scope_and_fact_updates(tmp_path, case):
    rows = []
    for branch in ("actual", "oracle"):
        store = evaluation.Store(tmp_path / branch)
        store.initialize()
        model = ScriptedReader()
        row = await evaluation.run_case(store, model, case, branch)
        assert "error" not in row, row
        assert row["answer_pass"] and row["fact_state_pass"], row
        assert row["real_platform_actions"] == 0 and row["role_paused_after"]
        assert any(stage["stage"] == "context_budget" for stage in row["stages"])
        assert any(stage["stage"] == "query_plan" for stage in row["stages"])
        if case["id"] == "other-owner":
            final = [material for material in model.contexts if material.get("event", {}).get("native_id") == "question"][0]
            assert "秘密城" not in json.dumps(final, ensure_ascii=False)
        rows.append(row)
    report = evaluation.summarize(rows, real_model=False)
    assert not report["real_model"] and not report["blind_evaluation"]
    assert report["branches"]["actual"]["passes"] == 1


def test_answer_scoring_rejects_unknown_citations_and_contradictory_abstention():
    case = CASES[0]
    answer = {"text": "灰色盒子第二层", "evidence_ids": ["imagined"], "abstain": False}
    assert not evaluation.score(case, answer, {"turn-real": {"m1"}})["answer_pass"]
    assert not evaluation.score(case, None, {})["answer_pass"]
    unknown = next(row for row in CASES if row["id"] == "unknown")
    assert not evaluation.score(unknown, {"text": "不知道，但它叫咪咪", "evidence_ids": [], "abstain": True}, {})["answer_pass"]


@pytest.mark.asyncio
async def test_call_cap_is_recorded_as_failure_and_always_pauses_synthetic_role(tmp_path):
    store = evaluation.Store(tmp_path / "data")
    store.initialize()
    row = await evaluation.run_case(store, ScriptedReader(), CASES[0], "actual", allowance=[0])
    assert row["error"] == "evaluation_limit" and not row["answer_pass"] and row["role_paused_after"]
