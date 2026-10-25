from copy import deepcopy

import pytest

from companion_core.context_budget import measure, pack
from companion_core.errors import AssistantError


def turn(identifier, text):
    return {"data": {"id": identifier, "content": text}, "version": "v1"}


def context():
    return {"event": {"text": "我现在住哪里"}, "persona": {"name": "固定人物", "rules": "不可删除的设定"},
            "foundation_memories": [{"data": {"id": "base", "content": "完整基础设定"}, "version": "base-v1"}],
            "memory": {"records": [], "catalog": [], "total_records": 0},
            "conversation": {"turns": [], "recalled": [], "summary": None, "total_turns": 0},
            "personal_facts": {"items": [{"id": "old", "attribute": "residence", "value": "上海", "status": "historical"},
                                         {"id": "current", "attribute": "residence", "value": "杭州", "status": "active"}],
                               "counts": {"active": 1, "historical": 1, "disputed": 0, "future": 0}}}


def test_final_budget_includes_instructions_schema_and_preserves_current_fact_and_fixed_material():
    original = context()
    original["conversation"]["recalled"] = [turn("critical", "住址更正原文：我已经搬到杭州")]
    original["conversation"]["turns"] = [turn("recent", "当前问题")]
    original["memory"]["records"] = [{"kind": "knowledge", **turn(f"k{i}", "大量无关材料" * 400)} for i in range(8)]
    before = deepcopy(original)
    selected, report = pack(original, {"reply_context_chars": 3500}, "规则" * 100, {"type": "object"})
    assert original == before
    assert selected["persona"] == original["persona"]
    assert selected["foundation_memories"] == original["foundation_memories"]
    assert any(item["id"] == "current" for item in selected["personal_facts"]["items"])
    assert selected["conversation"]["recalled"][0]["data"]["id"] == "critical"
    assert report["after"] == measure(selected, "规则" * 100, {"type": "object"})
    assert report["after"]["chars"] <= 3500 and report["limited"]
    assert report["sections"]["memory_records"]["kept"] == 0
    assert "杭州" not in str(report)  # diagnostic metadata only


def test_fixed_material_overflow_fails_without_silently_truncating_persona_or_current_message():
    value = context()
    value["persona"]["rules"] = "固定规则" * 1000
    with pytest.raises(AssistantError, match="未截断设定"):
        pack(value, {"reply_context_chars": 2000})


def test_utf8_token_estimate_and_output_reserve_are_both_enforced():
    value = context()
    value["memory"]["records"] = [{"kind": "knowledge", **turn("large", "中文正文" * 500)}]
    selected, report = pack(value, {"reply_context_tokens": 3500, "reply_output_reserve": 1000})
    assert report["after"]["estimated_tokens"] + 1000 <= 3500
    assert not selected["memory"]["records"]


def test_conflicting_facts_are_selected_as_one_group():
    value = context()
    value["personal_facts"] = {"items": [{"id": str(i), "attribute": "residence", "status": "disputed",
                                        "value": "争议值" * 150} for i in range(2)],
                               "counts": {"disputed": 2}}
    selected, _ = pack(value, {"reply_context_chars": 2100})
    assert len(selected["personal_facts"]["items"]) in {0, 2}
    assert selected["personal_facts"]["omitted"]["disputed"] == 2 - len(selected["personal_facts"]["items"])


def test_catalog_remains_a_fallback_when_full_record_cannot_fit_and_duplicates_are_removed_when_it_does():
    value = context()
    value["memory"]["records"] = [{"kind": "knowledge", **turn("a", "x" * 6000)}]
    value["memory"]["catalog"] = [{"kind": "knowledge", "id": "a", "version": "v1", "summary": "短摘要"}]
    small, _ = pack(value, {"reply_context_chars": 3500})
    assert not small["memory"]["records"] and small["memory"]["catalog"]
    large, report = pack(value, {"reply_context_chars": 15000})
    assert large["memory"]["records"] and not large["memory"]["catalog"]
    assert report["sections"]["catalog"]["duplicates"] == 1


def test_omitted_growth_disables_reflection_but_keeps_its_state_unchanged():
    value = context()
    value["private_growth"] = {"reflection_due": True, "pending_experiences": ["x" * 5000]}
    selected, _ = pack(value, {"reply_context_chars": 2500})
    assert selected["private_growth"] == {"reflection_due": False, "context_omitted": True}
    assert value["private_growth"]["reflection_due"]


def test_attachment_metadata_is_part_of_required_budget():
    value = context()
    value["event_material"] = {"text": "材料" * 5000, "images_read": 1}
    with pytest.raises(AssistantError):
        pack(value, {"reply_context_chars": 3000})


@pytest.mark.parametrize("partial", [False, True])
def test_recalled_old_recent_turn_only_deduplicates_when_evidence_is_complete(partial):
    value = context()
    value["conversation"]["turns"] = [turn(str(i), "完整原文" + str(i)) for i in range(4)]
    recalled = turn("0", "片段" if partial else "完整原文0")
    if partial:
        recalled["content_span"] = [0, 2]
    value["conversation"]["recalled"] = [recalled]
    selected, report = pack(value, {})
    older = [row for name in ("turns", "recalled") for row in selected["conversation"][name] if row["data"]["id"] == "0"]
    assert len(older) == (2 if partial else 1)
    assert report["sections"]["recent_turns"]["duplicates"] == (0 if partial else 1)
    if partial:
        assert any(row["data"]["content"] == "完整原文0" for row in older)
