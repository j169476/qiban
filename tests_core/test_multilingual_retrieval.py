import json
from pathlib import Path

import pytest

from companion_core.passages import LONG_RECORD_CHARS, split
from companion_core.retrieval import scores


@pytest.mark.parametrize("case_id", ["q05", "h03", "q35"])
@pytest.mark.parametrize("query_file", ["queries.json", "governance-queries.json"])
def test_known_paraphrases_stay_in_top_five_among_mixed_long_records(case_id, query_file):
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "tests_core/fixtures/retrieval/cases.json").read_text(encoding="utf-8"))
    long = json.loads((root / "tests/fixtures/recall_cases_v060.json").read_text(encoding="utf-8"))
    queries = json.loads((root / "tests_core/fixtures/retrieval" / query_file).read_text(encoding="utf-8"))
    docs = {doc["id"]: doc for doc in cases["documents"] + long["documents"]}
    records = [{"kind": "memories", "data": doc} for doc in docs.values()]
    records += [{"kind": "memories", "data": {"id": f"noise-{i}", "content": "普通资料与文件、视频数据和模型状态。"}}
                for i in range(1000)]
    spans = {("memories", doc["id"]): split(doc["content"]) for doc in docs.values()
             if len(doc["content"]) > LONG_RECORD_CHARS}
    case = next(case for case in cases["queries"] if case["id"] == case_id)
    ranked = scores(records, case["query"], queries[case["id"]], passages=spans)
    top = sorted(ranked, key=ranked.get, reverse=True)[:5]
    assert all(("memories", identifier) in top for identifier in case["relevant"])
