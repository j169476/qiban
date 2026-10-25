"""Translate platform metadata to reusable core selection inputs."""
from datetime import datetime, timedelta

from companion_core.algorithm_policy import record_route
from companion_core.selection import Candidate, choose, interests

from .interests import interest_profile


def select(engine, candidates, *, explore=False, query="", seed=""):
    store = engine.store
    config = {k: store.read(k) for k in ("companion", "persona", "growth", "preferences")}
    settings = config["companion"]["data"]
    profile = interest_profile(store)
    weights, negative = interests(config["persona"]["data"]["interests"],
                                  config["growth"]["data"]["interests"], profile["topics"])
    account = store.read("manifest")["data"]["account_uid"]
    since = engine.clock() - timedelta(days=settings.get("diversity_window_days", 7))
    history = sorted((r["data"] for r in store.records("browse_observations")[0]
                      if r["data"]["account_uid"] == account and r["data"]["read_at"]
                      and datetime.fromisoformat(r["data"]["read_at"]) >= since), key=lambda r: r["read_at"])
    result = choose([
        Candidate(c["bvid"], c["title"] + " " + c.get("description", ""), c.get("owner_uid", ""),
                  tuple(t for t in c.get("topics", []) if isinstance(t, str))) for c in candidates
    ], settings=settings, weights=weights, negative=negative,
        history=[Candidate(r["bvid"], r["title"], r["owner_uid"], tuple(r["topics"])) for r in history],
        explore=explore, seed=seed, query=query)
    record_route(store, "selection", result["route"], result["reason"], candidates=len(candidates),
                 exploration=explore, goal=bool(query))
    versions = [(k, None, r["version"]) for k, r in config.items()]
    return result, versions
