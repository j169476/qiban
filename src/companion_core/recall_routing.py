"""Conservative query-expansion routing; only query plans, never records, are cached."""
import hashlib
import json
import re

from .algorithm_policy import policy, record_route
from .errors import AssistantError
from .memory_maintenance import RECALL_PROMPT, RECALL_SCHEMA
from .selection import contains, normalize

CACHE_KEY = "algorithm-recall-cache:v1"


def acknowledgment(query):
    # Exact complete utterances only. Any additional subject, question or recall
    # request retains semantic expansion. Normal memory/history retrieval remains.
    value = query.strip().casefold().strip("。！!，,.~～ ")
    return value in {"你好", "您好", "早上好", "早安", "晚安", "谢谢", "谢谢你", "好的", "收到",
                     "hi", "hello", "thanks", "thank you", "good morning", "good night", "ok", "okay"}


def valid_queries(value):
    return (isinstance(value, list) and 1 <= len(value) <= 5
            and all(isinstance(q, str) and 0 < len(q.strip()) <= 100 for q in value))


def cached_plans(store, config, now):
    """One bounded state value. Eviction is deterministic and independent of file mtimes."""
    previous = store.state(CACHE_KEY, {})
    if not isinstance(previous, dict) or not config["cache_entries"]:
        return {}
    valid = {key: entry for key, entry in previous.items()
             if isinstance(entry, dict) and valid_queries(entry.get("queries"))
             and type(entry.get("at")) in (int, float)
             and 0 <= now - entry["at"] < config["cache_ttl_hours"] * 3600}
    return dict(sorted(valid.items(), key=lambda pair: (pair[1]["at"], pair[0]))[-config["cache_entries"]:])


async def resolve(store, query, *, scope, uids=(), eligible=True, remaining=2, generate, guard, clock):
    """A model call reserves one remaining slot for the actual content decision.

    There is deliberately no lexical-score confidence threshold: high overlap can
    be a distractor, especially for negation, paraphrases and multilingual facts.
    Only explicit record IDs and user-defined whole-query aliases bypass a new
    expansion. Other queries use the existing Codex expansion or its scoped cache.
    """
    guard()
    configurations = {kind: store.read(kind) for kind in ("companion", "persona", "preferences")}
    config = policy(configurations["companion"]["data"], "recall")
    aliases = []
    exact_alias = False
    if config["enabled"]:
        for term, values in config["aliases"].items():
            if contains(query, term):
                aliases.extend(values)
                exact_alias |= normalize(query) == normalize(term)
    aliases = list(dict.fromkeys(aliases))[:5]

    def finish(route, reason, queries=(), references=()):
        record_route(store, "recall", route, reason, expansions=len(queries))
        from .reply_trace import point

        point("query_plan", route=route, reason=reason, expansion_count=len(queries))
        return {"route": route, "reason": reason, "queries": list(queries), "references": list(references)}

    if not query.strip():
        return finish("local", "empty_query")
    if not eligible:
        return finish("local", "small_library", aliases)
    if acknowledgment(query):
        return finish("local", "conversational_acknowledgment", aliases)
    key = hashlib.sha256(json.dumps({
        "query": query.strip(), "scope": scope, "uids": sorted(set(uids)), "policy": config,
        "configs": {k: v["version"] for k, v in configurations.items() if k != "companion"},
        "prompt": RECALL_PROMPT, "schema": RECALL_SCHEMA,
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    now = clock().timestamp()
    if config["enabled"]:
        if exact_alias:
            return finish("algorithm", "configured_alias", aliases)
        # Record identifiers are not searchable prose. Resolve them explicitly,
        # retaining the caller's relationship scope and the normal file validator.
        references = []
        item_id = query.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item_id):
            for kind in store.profile.memory_collections:
                if kind == "relationships" and uids and item_id not in uids:
                    continue
                try:
                    store.read(kind, item_id)
                except AssistantError as exc:
                    if exc.code not in ("not_found", "invalid_data", "invalid_yaml"):
                        raise
                else:
                    references.append({"kind": kind, "id": item_id})
        if references:
            return finish("algorithm", "explicit_record_id", aliases, references)
        with store.lock:
            cache = cached_plans(store, config, now)
            # Also apply smaller limits/expiration when the next access is a hit.
            if cache != store.state(CACHE_KEY, {}):
                store.set_state(CACHE_KEY, cache)
            if key in cache:
                return finish("cache", "scoped_query_plan", cache[key]["queries"])
    if remaining < 2:
        return finish("local", "model_budget", aliases)
    try:
        result = await generate(RECALL_PROMPT, {"query": query}, RECALL_SCHEMA)
        if not isinstance(result, dict) or not valid_queries(result.get("queries")):
            raise AssistantError("model_output_invalid", "查询扩写格式无效")
    except AssistantError as exc:
        if exc.code not in ("model_failed", "model_timeout", "model_output_invalid"):
            raise
        guard()
        return finish("fallback", exc.code, aliases)
    guard()
    # Editable policy and persona changes invalidate a plan in flight as well as a cache key.
    if any(store.read(k)["version"] != v["version"] for k, v in configurations.items()):
        raise AssistantError("context_changed", "查询扩写期间配置已经变化")
    queries = list(dict.fromkeys([q.strip() for q in result["queries"]] + aliases))[:5]
    if config["enabled"] and config["cache_entries"]:
        with store.lock:
            cache = cached_plans(store, config, clock().timestamp())
            cache[key] = {"at": clock().timestamp(), "queries": queries}
            cache = dict(sorted(cache.items(), key=lambda pair: (pair[1]["at"], pair[0]))[-config["cache_entries"]:])
            store.set_state(CACHE_KEY, cache)
    return finish("model", "semantic_expansion", queries)
