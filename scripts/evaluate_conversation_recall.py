"""Synthetic raw-history evidence coverage; never opens a database or calls a model."""
import argparse
import hashlib
import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion_core import conversations

ROOT = Path(__file__).resolve().parents[1]
NOISE = (
    "今天看看天气，散步后回家。",
    "今天整理桌面上的书和笔，暂时没有别的安排。",
    "随便聊聊周末的电影，没有形成新的约定。",
    "A quiet afternoon reading a book and listening to music.",
)


class Archive:
    """Already-authorized, in-memory fixture. ACL/SQL/transport are tested separately."""
    def __init__(self, dataset, count):
        self.tid = conversations.thread_id("synthetic", "local", "test-conversation")
        docs = {row["position"]: row for row in dataset["documents"]}
        self.turns = []
        for index in range(count):
            row = docs.get(index, {})
            content = "随手写下无关背景。" * row.get("prefix_repeat", 0) + row.get("content", NOISE[index % len(NOISE)])
            assert len(content) <= 12000  # Match the archive recorder's per-part limit.
            stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat()
            data = {"id": conversations.identifier("turn", self.tid, index), "account_uid": "synthetic",
                "thread_id": self.tid, "channel": "local", "thread_key": "test-conversation", "content": content,
                "speaker": "user", "sender_uid": "synthetic-user", "status": "received", "occurred_at": stamp,
                "message_id": f"message-{index}", "reply_to": "", "part": 0, "sequence": index,
                "source": {"recorded_at": stamp, "reference": f"message-{index}"}}
            self.turns.append({"data": data, "version": hashlib.sha256(json.dumps(data).encode()).hexdigest()})
        self.summary = {"data": {"id": "summary", "account_uid": "synthetic", "thread_id": self.tid,
            "content": "之前聊过生活与安排，摘要未保留具体细节。", "previous": None,
            "turn_refs": [{"id": row["data"]["id"], "version": row["version"]} for row in self.turns[:-24]]},
            "version": "synthetic-summary-v1"}

    def read(self, kind, identifier=None):
        return ({"data": {"conversation_context_turns": 24, "conversation_context_chars": 12000}}
                if kind == "companion" else self.summary)

    def records(self, kind):
        return (self.turns if kind == "conversation_turns" else [self.summary]), []


def evaluate(dataset, count):
    store = Archive(dataset, count)
    reports = {}
    for mode in ("summary_and_recent", "raw_query", "fixed_expansions"):
        rows = []
        for case in dataset["queries"]:
            start = time.perf_counter()
            result = conversations.context(store, "synthetic", store.tid,
                query="" if mode == "summary_and_recent" else case["query"],
                queries=case["expansions"] if mode == "fixed_expansions" else ())
            elapsed = time.perf_counter() - start
            supplied = [*result["turns"], *result["recalled"]]
            positions = {row["data"]["sequence"] for row in supplied}
            body = "\n".join(row["data"]["content"] for row in supplied)
            relevant = set(case["positions"])
            complete = relevant <= positions and all(value in body for value in case["evidence"])
            if not relevant:
                complete = not result["recalled"]
            rows.append({"id": case["id"], "complete_evidence": complete,
                "target_recall": len(relevant & positions) / len(relevant) if relevant else None,
                "recalled_positions": [row["data"]["sequence"] for row in result["recalled"]],
                "context_chars": result["context_chars"], "seconds": round(elapsed, 6)})
        positive = [row for row in rows if row["target_recall"] is not None]
        durations = sorted(row["seconds"] for row in rows)
        reports[mode] = {"complete_positive_cases": sum(row["complete_evidence"] for row in positive),
            "positive_cases": len(positive), "mean_target_recall": statistics.mean(row["target_recall"] for row in positive),
            "absent_evidence_pass": all(row["complete_evidence"] for row in rows if row["target_recall"] is None),
            "max_context_chars": max(row["context_chars"] for row in rows),
            "p50_seconds": statistics.median(durations), "p95_seconds": durations[int((len(durations) - 1) * .95)],
            "cases": rows}
    return {"turns": count, "modes": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests_core/fixtures/conversation_recall_cases.json")
    parser.add_argument("--turns", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    if min(args.turns) < max(row["position"] for row in dataset["documents"]) + 50:
        parser.error("Each corpus must leave at least 50 recent turns after the oldest evidence fixture")
    report = {"synthetic": True, "blind_evaluation": False, "model_calls": 0, "production_used": False,
        "measurement": "In-memory evidence retrieval only; excludes database/ACL/model/transport latency and answer accuracy",
        "expansions": "Fixed authored inputs, not generated or validated live model expansions",
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "runs": [evaluate(dataset, count) for count in args.turns]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "runs": [
        {"turns": run["turns"], **{key: value["complete_positive_cases"] for key, value in run["modes"].items()}}
        for run in report["runs"]]}))


if __name__ == "__main__":
    main()
