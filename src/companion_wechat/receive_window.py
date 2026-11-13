"""Validate persisted source bounds without touching the native client."""
from copy import deepcopy

from .contracts import require
from .db_source import validate_checkpoint


def validate_window(window, snapshot):
    enrollment, checkpoint = snapshot["enrollment"], snapshot["checkpoint"]
    require(isinstance(window, dict) and set(window) == {"epoch", "sources"}
            and window["epoch"] == enrollment["epoch"] and isinstance(window["sources"], dict)
            and set(window["sources"]) == set(checkpoint["sources"]), "wechat_source_invalid")
    lower, upper = {}, {}
    for key, item in window["sources"].items():
        require(isinstance(item, dict) and set(item) == {"shard", "target_ref", "salt", "anchor", "upper"},
                "wechat_source_invalid")
        lower[key] = {k: v for k, v in item.items() if k != "upper"}
        upper[key] = {**lower[key], "anchor": item["upper"]}
    for sources in (lower, upper):
        validate_checkpoint({"schema_version": 1, "epoch": window["epoch"], "sources": sources}, enrollment)
    for key, item in window["sources"].items():
        current = checkpoint["sources"][key]
        require(all(current[k] == item[k] for k in ("shard", "target_ref", "salt")), "wechat_source_generation_changed")
        position = (current["anchor"] or {}).get("local_id", 0)
        require((item["anchor"] or {}).get("local_id", 0) <= position <= (item["upper"] or {}).get("local_id", 0),
                "wechat_source_regressed")
        for endpoint in (item["anchor"], item["upper"]):
            require(not endpoint or position != endpoint["local_id"] or current["anchor"] == endpoint,
                    "wechat_source_regressed")
    return deepcopy(window)


def complete(snapshot):
    window = snapshot.get("window")
    return window is not None and all(snapshot["checkpoint"]["sources"][key]["anchor"] == item["upper"]
                                      for key, item in window["sources"].items())
