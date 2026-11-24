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


def pack(context, settings, instructions="", schema=None, *, explain_sources=False):
    limits = {key: (settings or {}).get(key, value) for key, value in DEFAULTS.items()}
    if any(type(value) is not int or value <= 0 for value in limits.values()) or limits["reply_context_tokens"] <= limits["reply_output_reserve"]:
        raise AssistantError("context_budget", "统一上下文预算配置无效")
    result = deepcopy(context)
    documents = result.get('event_material', {}).pop('attachment_texts', [])
    if documents:
        from .media_text import append_excerpts, install_notice

        install_notice(result, documents)
    candidates, sections = [], {}
    choices = []

    def mark(section, item, reason, grouped):
        if not explain_sources:
            return
        for row in item if grouped else [item]:
            if len(choices) >= 100 or not isinstance(row, dict):
                return
            default_kind = {"current_facts": "memories", "fact_history": "memories",
                            "recent_turns": "conversation_turns", "recalled_turns": "conversation_turns",
                            "summary": "conversation_summaries"}.get(section)
            kind = row.get("kind") if section in {"memory_records", "catalog"} else default_kind
            identifier = row.get("data", {}).get("id") or row.get("id")
            if kind and identifier and row.get("version"):
                choices.append({"resource": kind + "/" + identifier, "version": row["version"],
                                "section": section, "reason": reason})
    from .context_workspace import LAYERS

    order = (settings or {}).get("workspace_order", list(LAYERS))
    # Only reorder optional layers amongst their original slots.
    priorities = dict(zip(order, (20, 50, 60, 70, 90, 94, 95), strict=True))
    disabled = (settings or {}).get("workspace_disabled", [])

    def candidate(section, priority, path, value, *, grouped=False):
        sections.setdefault(section, {"offered": 0, "kept": 0, "duplicates": 0})["offered"] += len(value) if grouped else 1
        if section in disabled:
            sections[section]["reason"] = "disabled"
            mark(section, value, "disabled", grouped)
            return
        candidates.append((priorities.get(section, priority), len(candidates), section, path, value, grouped))

    def nested(path):
        parent = result
        for key in path[:-1]:
            parent = parent[key]
        return parent, path[-1]

    # Destination evidence competes for the same budget as memory and conversation.
    # Keep the routing handles even when their optional excerpts do not fit.
    routing = result.get("role_activity", result)
    prefix = ("role_activity",) if "role_activity" in result else ()
    for index, contact in enumerate(routing.get("contacts", ())):
        if isinstance(contact, dict) and "known_context" in contact:
            fragments, contact["known_context"] = contact["known_context"], []
            for item in fragments:
                candidate("contact_context", 65, (*prefix, "contacts", index, "known_context"), item)
    for field in ("provided", "reviewed_fragments"):
        if field in result and isinstance(result[field], list):
            fragments, result[field] = result[field], []
            for item in sorted(fragments,key=lambda row: row.get("said_at",""),reverse=True):
                candidate(field, 12, (field,), item)

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
    stickers = result.get("event_material", {}).get("stickers")
    if stickers:
        offered, stickers["items"] = stickers.get("items", []), []
        for item in offered:
            candidate("stickers", 95, ("event_material", "stickers", "items"), item)
    files = result.get("event_material", {}).get("files")
    if files:
        offered, files["items"] = files.get("items", []), []
        for item in offered:
            candidate("files", 94, ("event_material", "files", "items"), item)
    result["context_selection"] = {"limited": True, "guidance": "未提供的资料不能当作不存在；仅依据本轮可见证据回答。"}

    def fits():
        usage = measure(result, instructions, schema)
        return (usage["chars"] <= limits["reply_context_chars"]
                and usage["estimated_tokens"] + limits["reply_output_reserve"] <= limits["reply_context_tokens"])

    # Metadata counters have bounded space even after recalculation at the end.
    if not fits():
        raise AssistantError("context_budget", "核心设定、当前消息和必要材料超过统一预算；请调整预算或缩短输入，未截断设定")
    fixed_usage = measure(result, instructions, schema)
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
            mark(section, value, "duplicate", grouped)
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
            mark(section, value, "kept", grouped)
        else:
            parent[key] = previous
            mark(section, value, "budget", grouped)
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
    if documents:
        def media_fits():
            usage = measure(result, instructions, schema)
            return (usage['chars'] + 512 <= limits['reply_context_chars']
                    and usage['estimated_tokens'] + 512 + limits['reply_output_reserve'] <= limits['reply_context_tokens'])

        media_report = append_excerpts(result, documents, media_fits)
        sections['attachment_text'] = media_report
        limited |= media_report['limited']
        result['context_selection']['limited'] = limited
    if not fits():
        raise AssistantError("context_budget", "上下文预算元数据超过限制，未提交模型")
    report = {"before": measure(context, instructions, schema), "after": measure(result, instructions, schema),
              "fixed": fixed_usage,
              "limits": limits, "sections": sections, "limited": limited,
              "token_estimator": "utf8_bytes_upper_estimate", "image_tokens_included": False}
    report["workspace_version"] = (settings or {}).get("workspace_version", "initial")
    if explain_sources:
        report["source_choices"] = choices
    for key, section in sections.items():
        if key != "attachment_text":
            section["skipped"] = section["offered"] - section["duplicates"] - section["kept"]
            if section["skipped"]:
                section.setdefault("reason", "budget")
    return result, report
