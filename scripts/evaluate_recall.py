"""Offline scale benchmark. Model query expansion is an explicit, separate optional input."""

import argparse
import inspect
import json
import statistics
import time
from pathlib import Path

from bili_assistant.storage import Store

DISTRACTORS = [
    "今天浏览了视频、截图和字幕，讨论作者与内容推荐，也记录了上次遇到的问题。没有形成新的结论。",
    "讨论了模型和数据、文件与缓存、接口和状态、时间与顺序；这是一条普通讨论记录，没有具体办法。",
    "周末活动包括拍照、面包、画画和拆解，记录大家的爱好与邀请。没有讨论技术细节。",
    "The video and graph show a model and data. A note about coverage, output, input and a queue.",
    "记忆文件、视频内容、旧内容、搜索结果、系统状态、读取缓存、实际观察、数据修改和发版问题。",
    "花与纸、作者与模型、喜欢的内容、面包与文件。推荐给大家看一看，也许后续有新的讨论。",
]


def build(store, dataset, count):
    store.initialize()
    docs = list(dataset["documents"])
    docs += [{"id": f"noise-{i:05}", "content": DISTRACTORS[i % len(DISTRACTORS)] + f" 记录序号 {i}。",
              "tags": ["日常", f"批次{i % 17}"]} for i in range(max(0, count - len(docs)))]
    for doc in docs:
        if store.path("memories", doc["id"]).exists():
            continue
        store.write("memories", {
            "schema_version": 1, **doc, "kind": "experience", "confidence": "observed", "locked": False,
            "source": {"type": "conversation", "reference": "synthetic-benchmark",
                       "recorded_at": "2025-01-01T00:00:00Z" if doc["id"].startswith("m") else "2026-10-20T00:00:00Z"},
        }, None, "benchmark", doc["id"])


def evaluate(store, dataset, expansions=None):
    rows = []
    for case in dataset["queries"]:
        query = " ".join([case["query"], *(expansions or {}).get(case["id"], [])])
        start = time.perf_counter()
        results = (store.search(case["query"], 10, queries=(expansions or {}).get(case["id"], []))
                   if "queries" in inspect.signature(store.search).parameters else store.search(query, 10))
        elapsed = time.perf_counter() - start
        ids = [r["data"]["id"] for r in results]
        relevant = set(case["relevant"])
        ranks = [i + 1 for i, key in enumerate(ids) if key in relevant]
        rows.append({"id": case["id"], "type": case["type"], "top_ids": ids,
                     "recall_at_1": len(relevant.intersection(ids[:1])) / len(relevant),
                     "recall_at_5": len(relevant.intersection(ids[:5])) / len(relevant),
                     "recall_at_10": len(relevant.intersection(ids)) / len(relevant),
                     "reciprocal_rank": 1 / min(ranks) if ranks else 0, "seconds": round(elapsed, 4)})
    durations = sorted(r["seconds"] for r in rows)
    return {"queries": len(rows), "recall_at_1": statistics.mean(r["recall_at_1"] for r in rows),
            "recall_at_5": statistics.mean(r["recall_at_5"] for r in rows),
            "recall_at_10": statistics.mean(r["recall_at_10"] for r in rows),
            "mrr_at_10": statistics.mean(r["reciprocal_rank"] for r in rows),
            "first_query_seconds": rows[0]["seconds"], "median_seconds": statistics.median(durations),
            "p95_seconds": durations[min(len(durations) - 1, int(len(durations) * .95))], "cases": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True, help="Dedicated benchmark data directory, never production")
    parser.add_argument("--cases", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/recall_cases.json")
    parser.add_argument("--records", type=int, default=3000)
    parser.add_argument("--expansions", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.data.name != "benchmark-data":
        parser.error("--data must name a dedicated benchmark-data directory")
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    store = Store(args.data)
    build(store, dataset, args.records)
    expansions = json.loads(args.expansions.read_text(encoding="utf-8")) if args.expansions else None
    report = {"records": args.records, "dataset": str(args.cases), "synthetic": True,
              "query_expansion": bool(expansions), **evaluate(store, dataset, expansions)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
