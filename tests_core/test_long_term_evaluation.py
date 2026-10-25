import importlib.util
from pathlib import Path


def test_absent_answers_are_not_reported_as_success_and_hallucinated_sources_fail():
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate_long_term_dialogue.py"
    spec = importlib.util.spec_from_file_location("dialogue_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = [{"id": "one", "category": "recall", "history": [{"id": "m1", "text": "钥匙在灰盒"}],
              "question": "钥匙", "evidence_ids": ["m1"], "answer_requirements": [["灰盒"]], "abstain": False}]
    assert module.evaluate(cases)["answer_cases_scored"] == 0
    wrong = {"one": {"answer": "灰盒", "abstain": False, "evidence_ids": ["m1", "imagined"]}}
    assert module.evaluate(cases, wrong)["answer_passes"] == 0
    right = {"one": {"answer": "灰盒", "abstain": False, "evidence_ids": ["m1"]}}
    assert module.evaluate(cases, right)["answer_passes"] == 1
