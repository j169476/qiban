"""Read-only acceptance sampler. Never starts a run, changes policy, or sends messages."""

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from bili_assistant.health import age
from bili_assistant.storage import Store, atomic_write, utcnow


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def sample(store, generation):
    run = store.state("run", {})
    health = store.state(f"health:{generation}", {})
    supervisor = store.state(f"supervisor:{generation}", {})
    with store.db() as db:
        actions = [dict(row) for row in db.execute(
            "SELECT kind,status,COUNT(*) count FROM actions WHERE generation=? GROUP BY kind,status",
            (generation,),
        )]
        models = {row[0]: json.loads(row[1]) for row in db.execute(
            "SELECT key,value FROM state WHERE key LIKE 'model-cycles:%'"
        )}
        videos = [dict(row) for row in db.execute(
            "SELECT account,day,COUNT(*) count FROM videos GROUP BY account,day"
        )]
    ages = [age(record) for record in (health, supervisor)]
    reads, issues = store.records("browse_observations")
    return {
        "at": utcnow(), "run": run,
        "heartbeat": health, "supervisor": supervisor,
        "ages_seconds": [round(value, 2) if math.isfinite(value) else None for value in ages],
        "actions": actions, "model_counts": models, "video_counts": videos,
        "read_counts": {row["data"]["id"]: row["data"]["read_count"] for row in reads},
        "read_record_issues": len(issues),
        "policy_version": store.read("policy")["version"],
        "fault": store.state(f"fault:{generation}"),
    }


def summarize(meta, observations, ended=False):
    first = meta.get("baseline", observations[0])
    latest = observations[-1]
    stamps = [datetime.fromisoformat(row["at"]) for row in observations]
    started = datetime.fromisoformat(meta["run"]["started_at"])
    elapsed = (stamps[-1] - started).total_seconds()
    gaps = [(right - left).total_seconds() for left, right in zip(stamps, stamps[1:])]
    gaps.append((stamps[0] - started).total_seconds())
    health_gaps = [
        row for row, stamp in zip(observations, stamps)
        if (stamp - started).total_seconds() > 90 and row["run"].get("status") == "active"
        and any(value is None or value > 90 for value in row["ages_seconds"])
    ]
    same = latest["run"].get("generation") == meta["run"]["generation"]
    deadline = same and latest["run"].get("reason") == "duration_complete"
    readonly = all(row["run"].get("read_only") for row in observations if
                   row["run"].get("generation") == meta["run"]["generation"])
    no_actions = all(not row["actions"] for row in observations)
    stable_policy = all(row["policy_version"] == meta["policy_version"] for row in observations)
    observed_full = elapsed >= meta["duration_seconds"] and max(gaps, default=0) <= 90
    new_reads = sum(
        max(0, count - first["read_counts"].get(key, 0)) for key, count in latest["read_counts"].items()
    )
    new_models = sum(
        max(0, count - first["model_counts"].get(key, 0)) for key, count in latest["model_counts"].items()
    )
    passed = (
        ended and deadline and readonly and no_actions and stable_policy and observed_full
        and not health_gaps and new_reads > 0 and new_models > 0
        and not any(row["read_record_issues"] for row in observations)
    )
    return {
        "status": "passed" if passed else "interrupted_or_needs_review" if ended else "running",
        "generation": meta["run"]["generation"], "started_at": meta["run"]["started_at"],
        "deadline_at": meta["run"]["deadline_at"], "last_observed_at": latest["at"],
        "elapsed_wall_seconds": round(elapsed, 2), "samples": len(observations),
        "max_sample_gap_seconds": round(max(gaps, default=0), 2),
        "unhealthy_samples": len(health_gaps), "deadline_reached": deadline,
        "read_only": readonly, "no_platform_actions": no_actions, "policy_unchanged": stable_policy,
        "new_actual_reads": new_reads, "new_model_calls": new_models,
        "initial_model_counts": first["model_counts"], "final_model_counts": latest["model_counts"],
        "initial_video_counts": first["video_counts"], "final_video_counts": latest["video_counts"],
        "last_run": latest["run"], "fault": latest["fault"],
        "note": "只读整日运行检查；不证明真实凭证刷新、断电恢复或任何平台写操作成功。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--run-file", required=True, type=Path)
    args = parser.parse_args()
    meta = json.loads(args.run_file.read_text(encoding="utf-8"))
    store = Store(args.data)
    folder = args.run_file.parent
    observations = []
    # A second observer must not silently replace a partial observation record.
    with (folder / "samples.jsonl").open("x", encoding="utf-8") as stream:
        while True:
            row = sample(store, meta["run"]["generation"])
            observations.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            ended = (
                row["run"].get("generation") != meta["run"]["generation"]
                or row["run"].get("status") != "active"
                or datetime.now(timezone.utc) > datetime.fromisoformat(meta["run"]["deadline_at"])
                and age(row["heartbeat"]) > 90
            )
            write_json(folder / "result.json", summarize(meta, observations, ended))
            if ended:
                return
            time.sleep(60)


if __name__ == "__main__":
    main()
