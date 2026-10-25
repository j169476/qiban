"""One deterministic text-input budget, applied after all reply material is assembled.

UTF-8 bytes are a deliberately conservative token estimate, not provider usage.
Whole evidence units are selected; fixed instructions/persona/current input are never clipped.
"""
import json
from copy import deepcopy

from .errors import AssistantError

DEFAULTS = {"reply_context_chars": 96000, "reply_context_tokens": 262144, "reply_output_reserve": 4096}
PROPERTIES = {
    "reply_context_chars": {"type": "integer", "minimum": 8000, "maximum": 1000000},
    "reply_context_tokens": {"type": "integer", "minimum": 16000, "maximum": 2000000},
    "reply_output_reserve": {"type": "integer", "minimum": 256, "maximum": 32768},
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def measure(context, instructions="", schema=None):
    raw = instructions + "\n" + encoded(context) + "\n" + encoded(schema or {})
    return {"chars": len(raw), "estimated_tokens": len(raw.encode("utf-8"))}


def pack(context, settings, instructions="", schema=None):
    limits = {key: (settings or {}).get(key, value) for key, value in DEFAULTS.items()}
    if any(type(value) is not int or value <= 0 for value in limits.values()) or limits["reply_context_tokens"] <= limits["reply_output_reserve"]:
        raise AssistantError("context_budget", "统一上下文预算配置无效")
    result = deepcopy(context)
    candidates, sections = [], {}

    def candidate(section, priority, path, value, *, grouped=False):
        sections.setdefault(section, {"offered": 0, "kept": 0, "duplicates": 0})["offered"] += len(value) if grouped else 1
        candidates.append((priority, len(candidates), section, path, value, grouped))

    def nested(path):
        parent = result
        for key in path[:-1]:
            parent = parent[key]
        return parent, path[-1]

    facts = result.get("personal_facts")
    if facts:
        items, facts["items"] = facts["items"], []
        # All supplied conflicting values of one attribute travel together.
        for attribute in dict.fromkeys(item["attribute"] for item in items):
            current = [item for item in items if item["attribute"] == attribute and item["status"] in {"active", "disputed"}]
            if current:
                candidate("current_facts", 0, ("personal_facts", "items"), current, grouped=True)
        for item in items:
            if item["status"] not in {"active", "disputed"}:
                candidate("fact_history", 40 if item["status"] == "future" else 80, ("personal_facts", "items"), item)
    conversation = result.get("conversation")
    if conversation:
        turns, recalled, summary = conversation.get("turns", []), conversation.get("recalled", []), conversation.get("summary")
        conversation.update(turns=[], recalled=[], summary=None, truncated=True)
        for index, item in enumerate(reversed(turns)):
            candidate("recent_turns", 5 if index < 2 else 30 + index, ("conversation", "turns"), item)
        for item in recalled:
            candidate("recalled_turns", 10, ("conversation", "recalled"), item)
        if summary:
            candidate("summary", 50, ("conversation", "summary"), summary)
    memory = result.get("memory")
    if memory:
        records, catalog = memory.get("records", []), memory.get("catalog", [])
        memory.update(records=[], catalog=[], catalog_truncated=True)
        for item in records:
            candidate("memory_records", 20, ("memory", "records"), item)
        for item in catalog:
            candidate("catalog", 90, ("memory", "catalog"), item)
    if "social_relations" in result:
        claims, result["social_relations"]["claims"] = result["social_relations"].get("claims", []), []
        result["social_relations"]["truncated"] = True
        for item in claims:
            candidate("relations", 60, ("social_relations", "claims"), item)
    if "private_growth" in result:
        growth = result["private_growth"]
        result["private_growth"] = {"reflection_due": False, "context_omitted": True}
        candidate("growth", 70, ("private_growth",), growth)
    result["context_selection"] = {"limited": True, "guidance": "未提供的资料不能当作不存在；仅依据本轮可见证据回答。"}

    def fits():
        usage = measure(result, instructions, schema)
        return (usage["chars"] <= limits["reply_context_chars"]
                and usage["estimated_tokens"] + limits["reply_output_reserve"] <= limits["reply_context_tokens"])

    # Metadata counters have bounded space even after recalculation at the end.
    if not fits():
        raise AssistantError("context_budget", "核心设定、当前消息和必要材料超过统一预算；请调整预算或缩短输入，未截断设定")
    for _, _, section, path, value, grouped in sorted(candidates):
        supplied_facts = {item["id"] for item in result.get("personal_facts", {}).get("items", [])}
        supplied_records = {(item["kind"], item["data"]["id"]) for item in result.get("memory", {}).get("records", [])
                            if not item.get("content_truncated")}
        duplicate = (section == "memory_records" and value["kind"] == "memories" and value["data"]["id"] in supplied_facts
            or section == "catalog" and ((value["kind"], value["id"]) in supplied_records
                                         or value["kind"] == "memories" and value["id"] in supplied_facts)
            or section in {"recalled_turns", "recent_turns"} and any(item["data"]["id"] == value["data"]["id"]
                and not item.get("content_truncated") and not item.get("content_span")
                for name in ("turns", "recalled") for item in result.get("conversation", {}).get(name, [])))
        if duplicate:
            sections[section]["duplicates"] += 1
            continue
        parent, key = nested(path)
        previous = parent[key]
        if isinstance(previous, list):
            parent[key] = [*previous, *(value if grouped else [value])]
        else:
            parent[key] = value
        # Leave room for accurate counts and truncation flags, never for extra prose.
        usage = measure(result, instructions, schema)
        if (usage["chars"] + 512 <= limits["reply_context_chars"]
                and usage["estimated_tokens"] + 512 + limits["reply_output_reserve"] <= limits["reply_context_tokens"]):
            sections[section]["kept"] += len(value) if grouped else 1
        else:
            parent[key] = previous
    limited = any(row["kept"] + row["duplicates"] < row["offered"] for row in sections.values())
    result["context_selection"]["limited"] = limited
    if facts:
        facts["omitted"] = {status: count - sum(item["status"] == status for item in facts["items"])
                            for status, count in facts.get("counts", {}).items()}
        facts["truncated"] = bool(context["personal_facts"].get("truncated") or any(facts["omitted"].values()))
    if conversation:
        selected = {item["data"]["id"] for item in conversation["turns"]}
        conversation["turns"] = [item for item in context["conversation"].get("turns", []) if item["data"]["id"] in selected]
        conversation["truncated"] = bool(context["conversation"].get("truncated") or len(conversation["turns"]) < len(turns))
        conversation["context_chars"] = len(encoded([conversation["turns"], conversation["recalled"], conversation["summary"]]))
    if memory:
        memory["catalog_truncated"] = bool(context["memory"].get("catalog_truncated") or len(memory["catalog"]) < len(catalog))
        memory["context_chars"] = len(encoded(memory["records"]))
        memory["catalog_chars"] = len(encoded(memory["catalog"]))
        memory.pop("total_context_chars", None)
    if "social_relations" in result:
        result["social_relations"]["truncated"] = bool(context["social_relations"].get("truncated")
            or len(result["social_relations"]["claims"]) < len(context["social_relations"].get("claims", [])))
    if not fits():
        raise AssistantError("context_budget", "上下文预算元数据超过限制，未提交模型")
    report = {"before": measure(context, instructions, schema), "after": measure(result, instructions, schema),
              "limits": limits, "sections": sections, "limited": limited,
              "token_estimator": "utf8_bytes_upper_estimate", "image_tokens_included": False}
    return result, report
