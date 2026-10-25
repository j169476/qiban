from copy import deepcopy

import pytest

from bili_assistant.api import call
from bili_assistant.cognition import packet, verify_versions
from bili_assistant.conversations import append, context, records, thread_id
from bili_assistant.errors import AssistantError
from bili_assistant.memory_maintenance import (
    INDEX_SCHEMA,
    RECALL_SCHEMA,
    SUMMARY_SCHEMA,
    best_summary,
    catalog,
    compression_job,
    index_job,
    maintain,
    save_compression,
    save_index,
    save_merges,
    valid_groups,
)
from bili_assistant.scheduler import Scheduler


def configure(role, **values):
    doc = role.store.read("companion")
    doc["data"].update(values)
    role.store.write("companion", doc["data"], doc["version"], "test")


def add_memories(role, template, count):
    for i in range(count):
        data = deepcopy(template)
        data.update(id=f"note-{i:03}", content=f"第 {i} 条资料")
        role.remember("memories", data)


def index_result(job, title="主题", aliases=()):
    return {"groups": [{"title": title, "summary": r["data"]["content"], "aliases": list(aliases),
                        "refs": [{"kind": r["kind"], "id": r["data"]["id"]}]}
                       for r in job["sources"]]}


def turns(role, count, start=0):
    for i in range(start, start + count):
        append(role.store, "123", "dm", "789", f"dm-{i:04}", f"第 {i} 条前文约定",
               "user", "789", "received", role.clock().isoformat(), sequence=i)
    return thread_id("123", "dm", "789")


def test_cumulative_summary_preserves_raw_and_invalidates_on_ancestor_edit(role):
    configure(role, conversation_summary_after=12, conversation_keep_recent=4)
    tid = turns(role, 16)
    first = compression_job(role)
    assert first["type"] == "compression"
    first_id = save_compression(role, first, {"summary": "前十二条的称呼约定和未解决问题"})
    assert save_compression(role, first, {"summary": "不能重复改写"}) == first_id
    initial = context(role.store, "123", tid)
    assert initial["summary"]["covered_turns"] == 12
    assert len(initial["turns"]) == 4 and not initial["truncated"]
    turns(role, 12, start=16)
    second = compression_job(role)
    save_compression(role, second, {"summary": "累计约定，包括最新纠正"})
    assert best_summary(role.store, "123", tid)["record"]["data"]["content"] == "累计约定，包括最新纠正"
    assert len(records(role.store, "123", tid)) == 28
    original = first["turns"][0]
    original["data"]["content"] = "用户本地改过了约定"
    role.store.write("conversation_turns", original["data"], original["version"], "user", original["data"]["id"])
    assert best_summary(role.store, "123", tid) is None
    assert context(role.store, "123", tid)["summary"] is None


def test_summary_cannot_commit_after_source_or_ancestor_changes(role):
    configure(role, conversation_summary_after=12, conversation_keep_recent=4)
    tid = turns(role, 16)
    job = compression_job(role)
    role.store.path("conversation_turns", job["turns"][0]["data"]["id"]).unlink()
    with pytest.raises(AssistantError, match="来源"):
        save_compression(role, job, {"summary": "过期摘要"})
    assert context(role.store, "123", tid)["summary"] is None


def test_summary_is_scoped_and_packet_checks_original_versions(role):
    configure(role, conversation_summary_after=12, conversation_keep_recent=4)
    tid = turns(role, 16)
    job = compression_job(role)
    save_compression(role, job, {"summary": "只属于用户789的约定"})
    assert context(role.store, "999", tid)["summary"] is None
    assert context(role.store, "123", thread_id("123", "dm", "555"))["summary"] is None
    _, versions = packet(role, {"events": [{"id": "dm-latest", "kind": "dm_reply", "uid": "789", "text": ""}]})
    assert any(kind == "conversation_summaries" for kind, _, _ in versions)
    source = job["turns"][0]
    source["data"]["content"] = "人工更正"
    role.store.write("conversation_turns", source["data"], source["version"], "user", source["data"]["id"])
    with pytest.raises(AssistantError):
        verify_versions(role.store, versions)


def test_all_library_index_exceeds_old_catalog_and_recalls_semantic_alias(role, memory):
    configure(role, memory_maintenance_min_records=1, catalog_limit=10)
    add_memories(role, memory, 121)
    job = index_job(role)
    result = index_result(job, "英文概念", ["液体火箭发动机", "推进系统"])
    saved = save_index(role, job, result)
    for _ in range(20):
        job = index_job(role)
        if job["type"] != "index":
            break
        save_index(role, job, index_result(job))
    stats = catalog(role.store)
    assert stats["indexed_records"] == stats["total_records"] == 121
    assert stats["pending_records"] == 0
    assert "火箭" not in memory["content"]
    matched = role.store.search("液体火箭发动机")
    assert matched and matched[0]["data"]["id"] in [f"note-{i:03}" for i in range(8)]
    assert len(catalog(role.store, cursor=120)["groups"]) == 1
    first = role.store.read("semantic_groups", saved[0])
    assert first["data"]["members"][0]["version"]


@pytest.mark.parametrize("expanded", [False, True])
def test_manual_source_change_or_delete_invalidates_semantics(role, memory, expanded):
    configure(role, memory_maintenance_min_records=1)
    add_memories(role, memory, 2)
    job = index_job(role)
    save_index(role, job, index_result(job, aliases=["自定义同义表达"]))
    record = role.store.read("memories", "note-000")
    record["data"]["content"] = "人工修订"
    role.store.write("memories", record["data"], record["version"], "user", "note-000")
    role.store.path("memories", "note-001").unlink()
    assert not role.store.search("无相关内容" if expanded else "自定义同义表达",
                                 queries=["自定义同义表达"] if expanded else [])
    assert catalog(role.store)["stale_groups"] == 2
    assert catalog(role.store)["pending_records"] == 1


@pytest.mark.parametrize("malformation", ["invent", "repeat", "omit"])
def test_index_requires_exact_grounded_partition(role, memory, malformation):
    configure(role, memory_maintenance_min_records=1)
    add_memories(role, memory, 2)
    job = index_job(role)
    result = index_result(job)
    if malformation == "invent":
        result["groups"][0]["refs"][0]["id"] = "invented"
    elif malformation == "repeat":
        result["groups"].append(deepcopy(result["groups"][0]))
    else:
        result["groups"].pop()
    with pytest.raises(AssistantError, match="恰好覆盖"):
        save_index(role, job, result)
    assert not role.store.records("semantic_groups")[0]


def test_merge_keeps_sources_and_detects_manually_changed_parent(role, memory):
    configure(role, memory_maintenance_min_records=1)
    add_memories(role, memory, 2)
    job = index_job(role)
    save_index(role, job, index_result(job))
    merge = index_job(role)
    left, right = merge["pairs"][0].values()
    proposal = {"left": left["data"]["id"], "right": right["data"]["id"], "title": "相同意思",
                "summary": "合并后的来源视图", "aliases": ["相关表达"]}
    merged_id = save_merges(role, merge, {"merges": [proposal]})[0]
    active, _ = valid_groups(role.store)
    assert len(active) == 1 and active[0]["data"]["id"] == merged_id
    assert len(role.store.records("memories")[0]) == 2
    left["data"]["content"] = "人工纠正的分组含义"
    left["data"]["locked"] = True
    role.store.write("semantic_groups", left["data"], left["version"], "user", left["data"]["id"])
    active, stale = valid_groups(role.store)
    assert stale == 1 and all(r["data"]["id"] != merged_id for r in active)
    assert any(r["data"]["locked"] for r in active)


def test_global_sweep_reaches_every_pair_and_can_resume(role, memory):
    configure(role, memory_maintenance_min_records=1)
    add_memories(role, memory, 8)
    job = index_job(role)
    save_index(role, job, index_result(job))
    visited = set()
    for _ in range(20):
        job = index_job(role)
        if not job:
            break
        assert job["type"] == "merge"
        for pair in job["pairs"]:
            key = frozenset(g["data"]["id"] for g in pair.values())
            assert key not in visited
            visited.add(key)
        save_merges(role, job, {"merges": []})
    assert len(visited) == 28
    assert role.store.state("semantic-sweep")["complete"]
    assert index_job(role) is None


def test_new_records_do_not_restart_completed_comparisons(role, memory):
    configure(role, memory_maintenance_min_records=1)
    add_memories(role, memory, 14)
    while (job := index_job(role))["type"] == "index":
        save_index(role, job, index_result(job))
    first = {frozenset(g["data"]["id"] for g in p.values()) for p in job["pairs"]}
    save_merges(role, job, {"merges": []})
    fresh = deepcopy(memory)
    fresh.update(id="later-note", content="持续进入的新资料")
    role.remember("memories", fresh)
    next_job = index_job(role)
    save_index(role, next_job, index_result(next_job))
    remaining = set()
    while (job := index_job(role)) is not None:
        pairs = {frozenset(g["data"]["id"] for g in p.values()) for p in job["pairs"]}
        assert not pairs & first and not pairs & remaining
        remaining |= pairs
        save_merges(role, job, {"merges": []})
    assert len(first | remaining) == 15 * 14 // 2


def test_large_library_screens_pairs_but_never_claims_unchecked_global_completion(role, memory):
    from datetime import timedelta

    configure(role, memory_maintenance_min_records=1)
    for i in range(40):
        data = deepcopy(memory)
        data.update(id=f"distinct-{i}", content=f"separate_subject_{i}")
        role.remember("memories", data)
    while (job := index_job(role))["type"] == "index":
        save_index(role, job, index_result(job, title="common"))
    assert job["sweep_progress"]["filtered"]
    assert len(job["pairs"]) == 4
    save_merges(role, job, {"merges": []})
    assert index_job(role) is None
    state = role.store.state("semantic-sweep")
    assert state["total_pairs"] == 780 and not state["complete"] and not state["exhausted"]
    first = {frozenset(g["data"]["id"] for g in p.values()) for p in job["pairs"]}
    fresh = deepcopy(memory)
    fresh.update(id="new-distinct", content="unrelated_addition")
    role.remember("memories", fresh)
    new_index = index_job(role)
    save_index(role, new_index, index_result(new_index, title="common"))
    assert index_job(role) is None  # New records cannot reset the global audit cooldown.
    initial = role.clock()
    role.clock = lambda: initial + timedelta(days=1)
    next_job = index_job(role)
    assert next_job["sweep_progress"]["audit_pairs"] == 4
    assert not first & {frozenset(g["data"]["id"] for g in p.values()) for p in next_job["pairs"]}


@pytest.mark.asyncio
async def test_maintenance_runs_in_real_cycle_and_uses_existing_quota_gate(role, memory, monkeypatch):
    configure(role, memory_maintenance_min_records=1)
    add_memories(role, memory, 2)
    scheduler = Scheduler(role, role.run()["generation"])
    schemas = []
    async def model(prompt, payload, schema, images=()):
        schemas.append(schema)
        return index_result({"sources": payload["sources"]})
    async def no_inbox(since):
        return []
    async def no_feed(**kwargs):
        return []
    role.adapter.inbox, role.adapter.feed = no_inbox, no_feed
    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert schemas == [INDEX_SCHEMA]
    assert catalog(role.store)["indexed_records"] == 2
    monkeypatch.setattr(scheduler, "remaining_model_calls", lambda: 0)
    assert await maintain(scheduler) is None
    assert len(schemas) == 1


@pytest.mark.asyncio
async def test_summary_and_recall_native_schemas_are_wired(role, memory, monkeypatch):
    configure(role, conversation_summary_after=12, conversation_keep_recent=4, memory_maintenance_min_records=1)
    turns(role, 16)
    scheduler = Scheduler(role, role.run()["generation"])
    async def summary(prompt, payload, schema, images=()):
        assert schema == SUMMARY_SCHEMA and len(payload["turns"]) == 12
        return {"summary": "称呼约定"}
    monkeypatch.setattr(scheduler, "model", summary)
    assert (await maintain(scheduler))["type"] == "compression"
    add_memories(role, memory, 1)
    job = index_job(role)
    save_index(role, job, index_result(job, aliases=["小灯笼"]))
    from test_cognition import decision
    calls = []
    async def recall(prompt, payload, schema, images=()):
        calls.append(schema)
        if schema == RECALL_SCHEMA:
            return {"queries": ["小灯笼"]}
        assert any(r["data"]["id"] == "note-000" for r in payload["memory"]["records"])
        return decision()
    monkeypatch.setattr(scheduler, "model", recall)
    await scheduler.decide({"events": [{"id": "new", "kind": "dm_reply", "uid": "789", "text": "之前的叫法"}]})
    assert calls[0] == RECALL_SCHEMA and len(calls) == 2


@pytest.mark.asyncio
async def test_catalog_permission_and_manual_expanded_retrieval(role, admin, memory):
    configure(role, memory_maintenance_min_records=1)
    add_memories(role, memory, 1)
    job = index_job(role)
    save_index(role, job, index_result(job, aliases=["同义表达"]))
    assert (await call(role, "memory_catalog", {}))["indexed_records"] == 1
    assert (await call(role, "memory_context", {"queries": ["同义表达"]}))["records"]
    with pytest.raises(AssistantError):
        await call(admin, "memory_catalog", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "model_failed", "model_timeout", "model_output_invalid"])
async def test_recall_budget_or_model_failure_preserves_raw_context(role, memory, monkeypatch, error):
    from test_cognition import decision

    configure(role, catalog_limit=10)
    add_memories(role, memory, 11)
    scheduler = Scheduler(role, role.run()["generation"])
    monkeypatch.setattr(scheduler, "remaining_model_calls", lambda: 2 if error else 1)
    calls = []
    async def model(prompt, payload, schema, images=()):
        calls.append(schema)
        if schema == RECALL_SCHEMA:
            raise AssistantError(error, "simulated expansion failure")
        assert any(r["data"]["id"] == "note-000" for r in payload["memory"]["records"])
        return decision()
    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.decide({"events": [{"id": "new", "kind": "dm_reply", "uid": "789", "text": "第 0 条资料"}]})
    assert len(calls) == (2 if error else 1)
    assert (RECALL_SCHEMA in calls) == bool(error)
