"""Reproducible offline routing/quality comparison. No model or platform is called."""
import argparse
import asyncio
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evaluate_recall import build, evaluate

from companion_core.algorithm_policy import policy
from companion_core.merge_candidates import plan
from companion_core.recall_routing import CACHE_KEY, resolve
from companion_core.selection import Candidate, choose
from companion_core.storage import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def metrics(rows):
    return {"queries": len(rows), **{key: statistics.mean(r[key] for r in rows)
            for key in ("recall_at_1", "recall_at_5", "recall_at_10", "reciprocal_rank")}, "cases": rows}


async def recall(destination, label, cases_path, expansion_path, records):
    dataset = json.loads(cases_path.read_text(encoding="utf-8"))
    expansions = json.loads(expansion_path.read_text(encoding="utf-8"))
    store = Store(destination / label / "benchmark-data")
    build(store, dataset, records)
    store.set_state(CACHE_KEY, {})
    baseline = evaluate(store, dataset, expansions)
    calls, runs = [], {}
    for mode in ("cold", "warm"):
        rows, routes = [], Counter()
        before = len(calls)
        for case in dataset["queries"]:
            async def generate(instructions, payload, schema):
                # The router sees the query only. Ground-truth IDs never enter this callback.
                assert payload == {"query": case["query"]}
                calls.append(case["id"])
                # The 0.6 long-context fixture saved an expansion for only one
                # cross-language case; the other cases use an identity stub.
                return {"queries": expansions.get(case["id"], [case["query"]])}

            routed = await resolve(store, case["query"], scope={"platform": "fixture", "account": "bot",
                                                               "thread": "recall-regression"},
                                   generate=generate, guard=lambda: None, clock=lambda: NOW)
            routes[routed["route"]] += 1
            found = store.search(case["query"], 10, queries=routed["queries"])
            ids = [r["data"]["id"] for r in found]
            relevant = set(case["relevant"])
            ranks = [i + 1 for i, key in enumerate(ids) if key in relevant]
            rows.append({"id": case["id"], "route": routed["route"], "top_ids": ids,
                         **{f"recall_at_{n}": len(relevant & set(ids[:n])) / len(relevant) for n in (1, 5, 10)},
                         "reciprocal_rank": 1 / min(ranks) if ranks else 0})
        runs[mode] = {**metrics(rows), "simulated_expansion_calls": len(calls) - before, "routes": dict(routes)}
    regressions = [{"id": old["id"], "mode": mode, "metric": key}
                   for mode in runs for old, new in zip(baseline["cases"], runs[mode]["cases"], strict=True)
                   for key in ("recall_at_1", "recall_at_5", "recall_at_10", "reciprocal_rank") if new[key] < old[key]]
    result = {"records": len(store.records("memories")[0]), "baseline": baseline,
              "identity_stub_queries": [c["id"] for c in dataset["queries"] if c["id"] not in expansions],
              **runs, "regressions": regressions,
              "legacy_expansion_calls_for_two_passes": len(dataset["queries"]) * 2,
              "routed_expansion_calls_for_two_passes": len(calls)}
    print(json.dumps({"recall": label, "queries": len(dataset["queries"]), "regressions": len(regressions),
                      "cold_routes": runs["cold"]["routes"], "warm_routes": runs["warm"]["routes"]}), flush=True)
    return result


def distribution(items):
    topics = Counter(t for item in items for t in item.topics)
    authors = Counter(item.author for item in items)
    shares = [count / len(items) for count in topics.values()]
    return {"reads": len(items), "topic_counts": dict(topics), "author_counts": dict(authors),
            "topic_coverage": len(topics), "author_coverage": len(authors),
            "max_topic_share": max(shares), "max_author_share": max(authors.values()) / len(items),
            "topic_entropy": -sum(p * math.log2(p) for p in shares)}


def exploration():
    """Normative reference honors scheduled exploration; it is not a recorded LLM."""
    results = {}
    for mode in ("reference", "hybrid"):
        history, decisions = [], []
        streak = 0
        for turn in range(120):
            forced = (turn + 1) % 5 == 0 or streak >= 2
            counts = Counter(t for item in history for t in item.topics)
            items = [Candidate(f"{turn}-{i}", f"{topic}相关内容", author, (topic,))
                     for i, (topic, author) in enumerate((("动画", "artist"), ("天文", "observer"), ("园艺", "gardener")))]
            # Fixed reference: ordinary rounds follow interest; exploration rotates
            # toward the least-read alternative. The rules predate any router scores.
            reference = min(items[1:], key=lambda c: (counts[c.topics[0]], c.id)) if forced else items[0]
            choice = choose(items, settings={"topic_max_share": .6}, weights={"动画": 1}, history=history,
                            explore=forced, seed=str(turn)) if mode == "hybrid" else {"route": "model", "selected": ""}
            selected = next(c for c in items if c.id == choice["selected"]) if choice["selected"] else reference
            decisions.append({"turn": turn, "exploration": forced, "route": choice["route"],
                              "topic": selected.topics[0], "author": selected.author})
            history.append(selected)
            current = distribution(history)
            concentrated = len(history) >= 8 and (current["max_topic_share"] > .6 or current["max_author_share"] > .5)
            streak = streak + 1 if concentrated else 0
        results[mode] = {**distribution(history), "routes": dict(Counter(d["route"] for d in decisions)),
                         "exploration_rounds": sum(d["exploration"] for d in decisions), "decisions": decisions}
    old, new = results["reference"], results["hybrid"]
    results["non_regression"] = (new["topic_coverage"] >= old["topic_coverage"]
                                 and new["author_coverage"] >= old["author_coverage"]
                                 and new["max_topic_share"] <= old["max_topic_share"]
                                 and new["max_author_share"] <= old["max_author_share"]
                                 and new["topic_entropy"] + 1e-12 >= old["topic_entropy"])
    results["reference_type"] = "deterministic exploration policy, not measured historical model behavior"
    return results


def merge_screening():
    groups = [{"version": "a" * 64, "data": {"id": f"g-{i:04}", "title": "common",
               "content": f"separate_topic_{i:04}", "aliases": [], "members": []}} for i in range(200)]
    groups[0]["data"]["content"] = groups[-1]["data"]["content"] = "spectroscopy quartz calibration"
    proposed = plan(groups, set(), {}, {}, NOW.timestamp())
    return {"groups": len(groups), "exhaustive_comparisons": proposed["total_pairs"],
            "first_window_comparisons": len(proposed["pairs"]), "global_audit_pairs": len(proposed["audit_keys"]),
            "duplicate_candidate_found": any({p[0]["data"]["id"], p[1]["data"]["id"]} == {"g-0000", "g-0199"}
                                             for p in proposed["pairs"]),
            "unrelated_pairs_are_still_unchecked": True, "semantic_merges_performed": 0,
            "policy": policy({}, "merge")}


async def main(args):
    files = [ROOT / "tests_core/fixtures/retrieval/cases.json", ROOT / "tests_core/fixtures/retrieval/queries.json",
             ROOT / "tests/fixtures/recall_cases_v060.json", ROOT / "tests_core/fixtures/retrieval/heldout-expansions.json"]
    report = {"synthetic": True, "real_model_calls": 0, "real_platform_calls": 0,
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
              "recall": {}}
    code = [*sorted((ROOT / "src/companion_core").glob("*.py")),
            ROOT / "src/bili_assistant/algorithm_inputs.py", ROOT / "src/bili_assistant/scheduler.py",
            ROOT / "src/bili_assistant/workflow.py", ROOT / "src/bili_assistant/learning.py",
            ROOT / "scripts/evaluate_algorithm_routing.py"]
    report["code_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in code}
    for label, paths in (("original64", files[:2]), ("long_context12", files[2:])):
        report["recall"][label] = await recall(args.data, label, *paths, args.records)
    report["exploration"] = exploration()
    report["merge_screening"] = merge_screening()
    report["passed"] = (all(not r["regressions"] for r in report["recall"].values())
                        and report["exploration"]["non_regression"]
                        and report["merge_screening"]["duplicate_candidate_found"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "output": str(args.output),
                      "exploration": {key: {k: v for k, v in value.items() if k != "decisions"}
                                      for key, value in report["exploration"].items() if isinstance(value, dict)},
                      "merge_screening": report["merge_screening"]}, ensure_ascii=False), flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Dedicated synthetic fixture directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records", type=int, default=3000)
    args = parser.parse_args()
    if args.data.name != "algorithm-benchmark" or args.records < 200:
        parser.error("Use a dedicated algorithm-benchmark directory and at least 200 records")
    asyncio.run(main(args))
