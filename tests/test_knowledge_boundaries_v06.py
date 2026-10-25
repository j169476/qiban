"""Regression coverage for versioned disputes and bounded conversation compression."""
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from test_cognition import configure
from test_knowledge import note, payload_for, result_for
from test_learning import SearchAdapter, goal, model_stub

from bili_assistant.cognition import packet, verify_versions
from bili_assistant.knowledge import annotations, save_review, source_for
from bili_assistant.learning import status, step
from bili_assistant.scheduler import Scheduler
from companion_core.conversations import append, context, records, thread_id
from companion_core.errors import AssistantError
from companion_core.knowledge import disputed_knowledge, latest_comparisons, latest_reviews, related_records
from companion_core.learning_tree import assessment_current, nodes_for, review_step
from companion_core.memory_maintenance import (
    best_summary,
    compression_job,
    compression_payload,
    maintain,
    save_compression,
)
from companion_core.profiles import default_profile


def compare(role, target, other, target_source, other_source, resolution="unresolved"):
    job = {"target": target, "related": [other], "method_trials": []}
    sources = [target_source, other_source]
    result = result_for(job, sources)
    comparison = result["comparisons"][0]
    comparison.update(relation="contradiction", resolution=resolution)
    if resolution != "unresolved":
        comparison["evidence"] = [
            {"source_id": s["data"]["id"], "quote": s["data"]["segments"][0]["text"]} for s in sources
        ]
    return save_review(role, job, sources, payload_for(job, sources), result)


async def prepared_goal(role, monkeypatch, stages):
    role.adapter = SearchAdapter()
    saved = goal(role)
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []
    monkeypatch.setattr(scheduler, "model", model_stub(role, calls))
    for _ in range(stages):
        await step(scheduler)
    current = role.store.read("learning_goals", saved["data"]["id"])
    learned = role.store.read("knowledge", current["data"]["knowledge_refs"][0]["id"])
    return scheduler, current, learned, source_for(role.store, learned, "123"), calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_both_directions_of_current_conflict_prevent_goal_completion(role, monkeypatch, reverse):
    scheduler, current, learned, source, _ = await prepared_goal(role, monkeypatch, 5)
    other, other_source = note(role, "counter-evidence", "BV1xx411c7mE", "同一实验条件下，同时改变多个参数。")
    compare(role, other, learned, other_source, source) if reverse else compare(role, learned, other, source, other_source)
    await step(scheduler)
    result = status(role, current["data"]["id"])["goals"][0]
    assert result["data"]["status"] == "active"
    assert not result["completion_current"]
    assert result["disputed_knowledge"] == [learned["data"]["id"]]
    assert "未决复查" in result["data"]["gaps"][0]


@pytest.mark.asyncio
async def test_new_reverse_conflict_invalidates_completed_goal_and_review_without_mutating_history(role, monkeypatch):
    scheduler, current, learned, source, calls = await prepared_goal(role, monkeypatch, 6)
    assert status(role, current["data"]["id"])["goals"][0]["completion_current"]
    other, other_source = note(role, "later-correction", "BV1xx411c7mE", "实验条件中原先遗漏了对照组。")
    compare(role, other, learned, other_source, source)
    saved = role.store.read("learning_goals", current["data"]["id"])
    result = status(role, current["data"]["id"])["goals"][0]
    assert result["data"]["status"] == "completed"  # Preserve the historical result.
    assert not result["completion_current"]
    assert all(not assessment_current(role.store, saved, node) for node in nodes_for(role.store, saved))
    now = role.clock()
    role.clock = lambda: now + timedelta(days=8)
    before = len(calls)
    assert await review_step(scheduler) is None
    assert len(calls) == before
    assert role.store.read("learning_goals", current["data"]["id"])["version"] == saved["version"]


def test_rotating_comparison_does_not_erase_prior_unresolved_pair(role):
    first, source_a = note(role, "a", "BV1xx411c7mD")
    other, source_b = note(role, "b", "BV1xx411c7mE", "应同时改变多个变量。")
    extras = [note(role, f"c{i}", f"BV1xx411c7m{i}", f"先测量第{i}号仪器噪声。") for i in range(3)]
    compare(role, first, other, source_a, source_b)
    now = role.clock()
    role.clock = lambda: now + timedelta(days=8)
    latest = latest_reviews(role.store, "123")[0]["a"]
    related = related_records(role.store, first, latest)
    assert "b" not in {r["data"]["id"] for r in related}
    job = {"target": first, "related": related, "method_trials": []}
    sources = [source_a, *(source for _, source in extras)]
    save_review(role, job, sources, payload_for(job, sources), result_for(job, sources))
    for record in (first, other):
        annotated = annotations(role.store, [{"kind": "knowledge", **deepcopy(record)}])[0]
        assert annotated["unresolved_conflict_count"] == 1
    assert disputed_knowledge(role.store, "123", [{"id": "a", "version": first["version"]}]) == ["a"]


def test_reverse_resolution_supersedes_only_that_versioned_pair(role):
    first, source_a = note(role, "a", "BV1xx411c7mD")
    other, source_b = note(role, "b", "BV1xx411c7mE", "另一种条件下需要改变两个参数。")
    compare(role, first, other, source_a, source_b)
    now = role.clock()
    role.clock = lambda: now + timedelta(seconds=1)
    compare(role, other, first, source_b, source_a, "both_conditional")
    assert len(latest_comparisons(role.store, "123")) == 1
    assert not disputed_knowledge(role.store, "123", [{"id": "a", "version": first["version"]}])
    annotated = annotations(role.store, [{"kind": "knowledge", **deepcopy(first)}])[0]
    assert annotated["conflict_review_count"] == 1
    assert annotated["unresolved_conflict_count"] == 0
    other["data"]["content"] = "本地修订后的条件"
    role.store.write("knowledge", other["data"], other["version"], "user", "b")
    assert not latest_comparisons(role.store, "123")


def test_pair_remains_valid_when_an_unrelated_compared_note_changes(role):
    first, source_a = note(role, "a", "BV1xx411c7mD")
    other, source_b = note(role, "b", "BV1xx411c7mE", "另一种条件下需要改变两个参数。")
    third, source_c = note(role, "c", "BV1xx411c7mF", "需要校准温度传感器。")
    job = {"target": first, "related": [other, third], "method_trials": []}
    sources = [source_a, source_b, source_c]
    result = result_for(job, sources)
    result["comparisons"][0].update(relation="contradiction", resolution="unresolved")
    save_review(role, job, sources, payload_for(job, sources), result)
    third["data"]["content"] = "另一条知识的独立修订"
    role.store.write("knowledge", third["data"], third["version"], "user", "c")
    assert disputed_knowledge(role.store, "123", [{"id": "a", "version": first["version"]}]) == ["a"]
    _, dependencies = packet(role, {"events": []})
    assert ("knowledge", "c", third["version"]) not in dependencies
    assert ("knowledge", "b", other["version"]) in dependencies
    verify_versions(role.store, dependencies)
    other["data"]["content"] += "本地补充"
    role.store.write("knowledge", other["data"], other["version"], "user", "b")
    with pytest.raises(AssistantError, match="变化"):
        verify_versions(role.store, dependencies)
    assert not disputed_knowledge(role.store, "123", [{"id": "a", "version": first["version"]}])


def add_turns(role, count=30, long_first=True, start=0):
    for i in range(start, start + count):
        content = ("引言" * 1500 + "关键的后半段" * 500) if i == 0 and long_first else f"第 {i} 条短消息"
        append(role.store, "123", "codex", "role", f"message-{i}", content,
               "user", "", "received", role.clock().isoformat(), sequence=i)
    return thread_id("123", "codex", "role")


def test_long_turn_is_compressed_in_exact_budgeted_spans_without_hiding_tail(role):
    configure(role, context_char_limit=4000, conversation_summary_after=12, conversation_keep_recent=4,
              conversation_context_turns=60)
    tid = add_turns(role)
    first = compression_job(role)
    span = first["spans"][0]
    assert span["start"] == 0 < span["end"] < span["total"]
    assert len(json.dumps(compression_payload(first), ensure_ascii=False)) <= 4000
    save_compression(role, first, {"summary": "首条消息开头的部分摘要；后文待续。"})
    chain = best_summary(role.store, "123", tid)
    assert chain["covered"] == [] and chain["partial"]["end"] == span["end"]
    visible = context(role.store, "123", tid, max_chars=100000)
    original = records(role.store, "123", tid)[0]
    assert visible["turns"][0]["data"]["content"] == original["data"]["content"][span["end"]:]
    assert visible["summary"]["covered_turns"] == 0
    previous = chain["progress"]
    for _ in range(20):
        job = compression_job(role)
        if job is None:
            break
        assert len(json.dumps(compression_payload(job), ensure_ascii=False)) <= 4000
        item_id = save_compression(role, job, {"summary": "连续累计摘要，保留后半段和未解决问题。"})
        saved = role.store.read("conversation_summaries", item_id)
        default_profile().validate("conversation_summaries", saved["data"])
        chain = best_summary(role.store, "123", tid)
        assert chain["progress"] > previous
        previous = chain["progress"]
    assert len(chain["covered"]) > 1
    assert len(records(role.store, "123", tid)) == 30


def test_partial_summary_invalidates_on_edit_and_rejects_gapped_coverage(role):
    configure(role, context_char_limit=4000, conversation_summary_after=12, conversation_keep_recent=4)
    tid = add_turns(role)
    job = compression_job(role)
    forged = deepcopy(job)
    forged["spans"][0]["start"] = 1
    forged["turns"][0]["data"]["content"] = forged["turns"][0]["data"]["content"][1:]
    with pytest.raises(AssistantError, match="覆盖必须连续"):
        save_compression(role, forged, {"summary": "跳过一个字符"})
    save_compression(role, job, {"summary": "合法前半段"})
    next_job = compression_job(role)
    original = role.store.read("conversation_turns", job["turns"][0]["data"]["id"])
    original["data"]["content"] += "用户更正"
    role.store.write("conversation_turns", original["data"], original["version"], "user", original["data"]["id"])
    assert best_summary(role.store, "123", tid) is None
    with pytest.raises(AssistantError, match="变化"):
        save_compression(role, next_job, {"summary": "旧后半段"})


@pytest.mark.parametrize("additional", [0, 12])
def test_old_oversized_summary_rebuilds_bounded_chain_from_originals(role, additional):
    configure(role, conversation_summary_after=12, conversation_keep_recent=4)
    tid = add_turns(role, count=16, long_first=False)
    first = compression_job(role)
    old_id = save_compression(role, first, {"summary": "旧摘要" * 2000})
    configure(role, context_char_limit=4000)
    add_turns(role, count=additional, long_first=False, start=16)
    rebuilding = compression_job(role)
    assert rebuilding["previous"] is None
    for _ in range(20):
        job = compression_job(role)
        if job is None:
            break
        assert len(json.dumps(compression_payload(job), ensure_ascii=False)) <= 4000
        save_compression(role, job, {"summary": "基于完整原始范围重建的累计摘要"})
    current = best_summary(role.store, "123", tid)
    assert len(current["covered"]) >= 12
    assert current["record"]["data"]["id"] != old_id
    assert role.store.read("conversation_summaries", old_id)["data"]["content"] == "旧摘要" * 2000


@pytest.mark.asyncio
async def test_small_budget_summary_output_is_bounded_for_next_call(role, monkeypatch):
    configure(role, context_char_limit=4000, conversation_summary_after=12, conversation_keep_recent=4)
    add_turns(role)
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(prompt, payload, schema, images=()):
        assert len(json.dumps(payload, ensure_ascii=False)) <= 4000
        assert schema["properties"]["summary"]["maxLength"] <= 1000
        return {"summary": "首段摘要"}

    monkeypatch.setattr(scheduler, "model", model)
    assert (await maintain(scheduler))["type"] == "compression"
    assert compression_job(role)["previous"] is not None
