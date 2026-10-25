"""Platform-independent knowledge validity, conflicts and method assessment."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import AssistantError
from .storage import recorded_time, search_terms


def ref(record):
    return {"id": record["data"]["id"], "version": record["version"]}


def same_versions(store, kind, references):
    try:
        return all(store.read(kind, r["id"])["version"] == r["version"] for r in references)
    except AssistantError:
        return False


def valid_review(store, record, account):
    doc = record["data"]
    return (
        doc["account_uid"] == account
        and same_versions(store, "knowledge", [doc["target"], *doc["related"]])
        and same_versions(store, "source_snapshots", doc["sources"])
        and same_versions(store, "method_trials", doc["method_trials"])
    )


def latest_reviews(store, account):
    latest, stale = {}, 0
    for record in sorted(store.records("knowledge_reviews")[0], key=lambda r: recorded_time(r["data"])):
        if record["data"]["account_uid"] != account:
            continue
        if valid_review(store, record, account):
            latest[record["data"]["target"]["id"]] = record
        else:
            stale += 1
    return latest, stale


def latest_comparisons(store, account):
    """Keep the latest judgment for each *pair of versions*, in either direction.

    A review's overall assessment belongs to its target, whereas an omitted pair
    was not reconsidered. Rotating the comparison batch must not erase a prior
    disagreement. The original review remains the provenance of each judgment.
    """
    latest = {}
    for record in sorted(store.records("knowledge_reviews")[0], key=lambda r: recorded_time(r["data"])):
        doc = record["data"]
        if (doc["account_uid"] != account
                or not same_versions(store, "source_snapshots", doc["sources"])
                or not same_versions(store, "method_trials", doc["method_trials"])):
            continue
        related = {reference["id"]: reference for reference in doc["related"]}
        for comparison in doc["result"]["comparisons"]:
            other = related.get(comparison["other_id"])
            if not other or not same_versions(store, "knowledge", [doc["target"], other]):
                continue
            pair = tuple(sorted((r["id"], r["version"]) for r in (doc["target"], other)))
            latest[pair] = {"review": record, "comparison": comparison,
                            "target": doc["target"], "other": other}
    return latest


def disputed_knowledge(store, account, references):
    """Shared completion/recall rule: unresolved conflicts are bidirectional."""
    selected = {reference["id"] for reference in references}
    reviews, _ = latest_reviews(store, account)
    disputed = {item_id for item_id in selected if item_id in reviews
                and reviews[item_id]["data"]["result"]["assessment"] != "supported_by_material"}
    for entry in latest_comparisons(store, account).values():
        comparison = entry["comparison"]
        if comparison["relation"] == "contradiction" and comparison["resolution"] == "unresolved":
            disputed.update(selected & {entry["target"]["id"], entry["other"]["id"]})
    return sorted(disputed)


def trial_status(store, skill, account):
    trials = []
    for record in store.records("method_trials")[0]:
        doc = record["data"]
        evidence_kind = "conversation_turns" if doc["mode"] == "user_report" else "knowledge"
        if (
            doc["account_uid"] == account and doc["skill"] == ref(skill)
            and same_versions(store, evidence_kind, [doc["evidence"]])
        ):
            trials.append(record)
    trials.sort(key=lambda r: recorded_time(r["data"]), reverse=True)
    return {
        "level": "user_report" if any(t["data"]["mode"] == "user_report" for t in trials)
        else "simulation" if trials else "extracted_only",
        "requires_review": any(t["data"]["outcome"] == "failed" for t in trials),
        "trials": trials[:5], "total_trials": len(trials),
        "independently_executed": False,
    }


def annotations(store, records):
    account = store.read("manifest")["data"]["account_uid"]
    reviews, _ = latest_reviews(store, account)
    comparisons = latest_comparisons(store, account)
    for record in records:
        if record["kind"] == "knowledge":
            record["review"] = reviews.get(record["data"]["id"])
            # Both ends see the same current pair verdict, including old pairs
            # not selected in the latest review batch.
            conflicts = [entry for entry in comparisons.values()
                         if entry["comparison"]["relation"] == "contradiction"
                         and record["data"]["id"] in (entry["target"]["id"], entry["other"]["id"])]
            provenance = {}
            for entry in conflicts:
                review = entry["review"]
                view = provenance.setdefault(review["data"]["id"], {
                    **review, "knowledge_dependencies": [], "active_comparisons": [],
                })
                for dependency in (entry["target"], entry["other"]):
                    if dependency not in view["knowledge_dependencies"]:
                        view["knowledge_dependencies"].append(dependency)
                view["active_comparisons"].append(entry["comparison"])
            record["conflict_reviews"] = sorted(
                provenance.values(), key=lambda r: recorded_time(r["data"]), reverse=True)[:3]
            record["conflict_review_count"] = len(conflicts)
            record["unresolved_conflict_count"] = sum(
                entry["comparison"]["resolution"] == "unresolved" for entry in conflicts)
            record["conflict_pairs"] = [{"target": entry["target"], "other": entry["other"],
                                         "comparison": entry["comparison"], "review": ref(entry["review"])}
                                        for entry in conflicts]
        elif record["kind"] == "skills":
            record["validation"] = trial_status(store, record, account)
    return records


def review_usage(engine):
    policy = engine.store.read("policy")["data"]
    day = engine.clock().astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
    account = engine.store.read("manifest")["data"]["account_uid"]
    key = f"knowledge-review:{account}:{day}"
    limit = engine.store.read("companion")["data"].get("review_daily_limit", 2)
    used = engine.store.state(key, 0)
    return {"key": key, "day": day, "used": used, "limit": limit, "remaining": max(0, limit - used)}


def related_records(store, target, latest):
    others = [r for r in store.records("knowledge")[0] if r["data"]["id"] != target["data"]["id"]]
    terms = search_terms(target["data"]["content"] + " " + " ".join(target["data"]["tags"]))
    explicit = target["data"]["disagrees_with"]
    checked = {r["id"] for r in latest["data"]["related"]} if latest else set()

    def rank(record):
        doc = record["data"]
        overlap = sum(t in (doc["content"] + " ".join(doc["tags"])).casefold() for t in terms)
        return doc["id"] in explicit, doc["id"] not in checked, overlap, recorded_time(doc)

    return sorted(others, key=rank, reverse=True)[:3]


def review_job(engine):
    store = engine.store
    account = store.read("manifest")["data"]["account_uid"]
    reviews, _ = latest_reviews(store, account)
    interval = store.read("companion")["data"].get("review_interval_days", 7)
    failures = {}
    for skill in store.records("skills")[0]:
        for trial in trial_status(store, skill, account)["trials"]:
            if trial["data"]["outcome"] == "failed":
                for item_id in skill["data"]["knowledge_ids"]:
                    failures.setdefault(item_id, []).append(trial)
    candidates = []
    for target in store.records("knowledge")[0]:
        item_id = target["data"]["id"]
        latest = reviews.get(item_id)
        trials = sorted(failures.get(item_id, []), key=lambda r: recorded_time(r["data"]), reverse=True)[:5]
        new_failure = any(ref(t) not in (latest["data"]["method_trials"] if latest else []) for t in trials)
        due = not latest or engine.clock() >= datetime.fromisoformat(
            latest["data"]["source"]["recorded_at"]
        ) + timedelta(days=1 if latest["data"]["result"]["assessment"] != "supported_by_material" else interval)
        if due or new_failure:
            priority = (new_failure, bool(target["data"]["disagrees_with"]), not latest)
            candidates.append((priority, -recorded_time(target["data"]), target, latest, trials))
    if not candidates:
        return None
    _, _, target, latest, trials = max(candidates, key=lambda c: (c[0], c[1], c[2]["data"]["id"]))
    return {"target": target, "related": related_records(store, target, latest), "method_trials": trials}


def status(engine):
    engine.mode()
    account = engine.store.read("manifest")["data"]["account_uid"]
    reviews, stale = latest_reviews(engine.store, account)
    return {
        "quota": review_usage(engine), "knowledge_count": len(engine.store.records("knowledge")[0]),
        "valid_reviews": len(reviews), "stale_reviews": stale,
        "needs_evidence": sum(r["data"]["result"]["assessment"] != "supported_by_material" for r in reviews.values()),
        "unresolved_conflicts": sum(
            entry["comparison"]["relation"] == "contradiction"
            and entry["comparison"]["resolution"] == "unresolved"
            for entry in latest_comparisons(engine.store, account).values()),
        "next_target": (review_job(engine) or {}).get("target"),
    }


def validate_quotes(evidence, sources):
    cited = set()
    for citation in evidence:
        source = sources.get(citation["source_id"])
        if not source or not citation["quote"].strip() or not any(
            citation["quote"] in s["text"] for s in source["segments"]
        ):
            raise AssistantError("ungrounded_review", "复查引用必须逐字存在于实际提供的来源片段")
        cited.add(citation["source_id"])
    return cited
