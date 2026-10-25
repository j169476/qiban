"""Frozen synthetic dialogue cases: separate retrieval evidence from supplied answer quality.

No production data, implicit model calls or embeddings. --answers accepts JSONL
from a separately authorized model run, so missing answers never count as passes.
"""
import argparse
import hashlib
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion_core.conversation_recall import recall

ROOT = Path(__file__).resolve().parents[1]


def evaluate(cases, answers=None):
    reports = []
    for case in cases:
        records = []
        placed = {7 + index * 25: message for index, message in enumerate(case["history"])}
        for index in range(120):
            message = placed.get(index, {"id": f"noise-{index}", "text": "散步结束后整理桌面上的纸张。"})
            stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)).isoformat()
            data = {"id": message["id"], "content": message["text"], "speaker": message.get("speaker", "user"),
                    "sender_uid": "synthetic-user", "status": message.get("status", "received"),
                    "occurred_at": stamp, "sequence": index,
                    "message_id": message["id"], "part": 0, "reply_to": "",
                    "source": {"recorded_at": stamp}}
            records.append({"data": data, "version": hashlib.sha256(json.dumps(data).encode()).hexdigest()})
        start = time.perf_counter()
        selected = recall(records, case["question"])
        elapsed = round((time.perf_counter() - start) * 1000, 3)
        ids = {record["data"]["id"] for record in selected}
        answer = (answers or {}).get(case["id"])
        score = None
        if answer is not None:
            text = answer.get("answer", "")
            citations = answer.get("evidence_ids", [])
            abstain = answer.get("abstain") is True
            score = (abstain and not citations if case["abstain"] else
                     not abstain and all(any(term.casefold() in text.casefold() for term in alternatives)
                                         for alternatives in case["answer_requirements"])
                     and set(case["evidence_ids"]) <= set(citations) <= ids)
        reports.append({"id": case["id"], "category": case["category"],
                        "evidence_complete": set(case["evidence_ids"]) <= ids if not case["abstain"] else None,
                        "retrieved_ids": sorted(ids), "retrieval_ms": elapsed,
                        "answer_pass": score, "abstention_case": case["abstain"]})
    categories = defaultdict(list)
    for row in reports:
        categories[row["category"]].append(row)
    return {"case_count": len(reports), "answer_cases_scored": sum(row["answer_pass"] is not None for row in reports),
            "answer_passes": sum(row["answer_pass"] is True for row in reports),
            "categories": {key: {"cases": len(rows),
                "evidence_complete": sum(row["evidence_complete"] is True for row in rows),
                "answer_passes": sum(row["answer_pass"] is True for row in rows)} for key, rows in categories.items()},
            "retrieval_p50_ms": statistics.median(row["retrieval_ms"] for row in reports), "cases": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests_core/fixtures/long_term_dialogue.json")
    parser.add_argument("--answers", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.cases.read_bytes()
    dataset = json.loads(raw)
    answers = None
    if args.answers:
        rows = [json.loads(line) for line in args.answers.read_text(encoding="utf-8").splitlines() if line.strip()]
        answers = {row["id"]: row for row in rows}
        if len(answers) != len(rows) or set(answers) - {case["id"] for case in dataset["cases"]}:
            parser.error("Answer IDs must be unique and belong to this exact case set")
    report = {"synthetic": True, "blind_evaluation": False, "model_calls": 0, "production_used": False,
              "cases_sha256": hashlib.sha256(raw).hexdigest(),
              "limits": "Lexical retrieval only; answer scoring is a conservative phrase/citation check, not a semantic judge. "
                        "No answer scores without supplied answers. ACL and database paths are tested separately.",
              **evaluate(dataset["cases"], answers)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "case_count": report["case_count"],
                      "answer_cases_scored": report["answer_cases_scored"]}))


if __name__ == "__main__":
    main()
