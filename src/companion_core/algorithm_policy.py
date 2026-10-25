"""Editable, platform-neutral policy for bounded algorithm/model routing."""
from copy import deepcopy

from .observability import measure


def object_schema(properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


def number(low, high, *, integer=False):
    return {"type": "integer" if integer else "number", "minimum": low, "maximum": high}


DEFAULTS = {
    "selection": {"enabled": True, "interest_weight": 3.0, "novelty_weight": 1.0,
                  "topic_penalty": 2.0, "author_penalty": 1.0, "min_margin": 0.75, "history_limit": 100},
    "recall": {"enabled": True, "cache_entries": 256, "cache_ttl_hours": 168, "aliases": {}},
    "merge": {"enabled": True, "neighbors_per_group": 4, "audit_pairs": 4,
              "exhaustive_below": 24, "global_audit_hours": 24},
}
SCHEMA = object_schema({
    "selection": object_schema({
        "enabled": {"type": "boolean"},
        **{key: number(0, 10) for key in ("interest_weight", "novelty_weight", "topic_penalty", "author_penalty")},
        "min_margin": number(0.1, 10), "history_limit": number(10, 500, integer=True),
    }),
    "recall": object_schema({
        "enabled": {"type": "boolean"}, "cache_entries": number(0, 1000, integer=True),
        "cache_ttl_hours": number(1, 720, integer=True),
        "aliases": {"type": "object", "maxProperties": 200,
                    "propertyNames": {"type": "string", "minLength": 2, "maxLength": 100},
                    "additionalProperties": {"type": "array", "minItems": 1, "maxItems": 5,
                                             "items": {"type": "string", "minLength": 2, "maxLength": 100}}},
    }),
    "merge": object_schema({
        "enabled": {"type": "boolean"}, "neighbors_per_group": number(1, 20, integer=True),
        "audit_pairs": number(1, 12, integer=True), "exhaustive_below": number(2, 100, integer=True),
        "global_audit_hours": number(1, 168, integer=True),
    }),
})


def policy(settings, section):
    result = deepcopy(DEFAULTS[section])
    result.update(settings.get("algorithm_routing", {}).get(section, {}))
    return result


def record_route(store, operation, route, reason, **counts):
    """Count routing separately from actual model usage; retain no query or prompt."""
    store.audit("routing", {"operation": operation, "route": route, "reason": reason, **counts})
    with measure(store, f"routing.{operation}.{route}.{reason}", kind="routing"):
        pass
