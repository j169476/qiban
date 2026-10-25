from copy import deepcopy
from datetime import timedelta

import pytest
from test_cognition import LearningAdapter, configure, material

from bili_assistant.api import call
from bili_assistant.conversations import append
from bili_assistant.errors import AssistantError
from bili_assistant.knowledge import (
    annotations,
    latest_reviews,
    maintain,
    record_trial,
    ref,
    review_job,
    save_review,
    snapshot,
    source_excerpt,
    source_for,
    trial_status,
)
from bili_assistant.scheduler import Scheduler


def note(role, item_id="note", bvid="BV1xx411c7mD", content="先运行最小案例，再修改一个参数观察结果。"):
    video = material(bvid)
    video["segments"][0]["text"] = content
    role.store.set_state(f"context:{bvid}", video)
    source = snapshot(role, video)
    saved = role.remember("knowledge", {
        "schema_version": 1, "id": item_id, "content": content,
        "source": {"type": "video", "recorded_at": role.clock().isoformat(), "reference": video["url"]},
        "coverage": video["coverage"], "tags": ["编程"], "disagrees_with": [], "locked": False,
    })
    return saved, source


def result_for(job, sources):
    target = job["target"]["data"]
    source = next(s for s in sources if s["data"]["bvid"] == target["coverage"]["bvid"])
    return {
        "summary": "材料说明此方法的适用场景，尚未独立实测。",
        "assessment": "supported_by_material",
        "claims": [{
            "claim": target["content"], "kind": "method", "assessment": "supported_by_material",
            "conditions": "适用于最小可复现实验。",
            "evidence": [{"source_id": source["data"]["id"], "quote": source["data"]["segments"][0]["text"]}],
        }],
        "comparisons": [{"other_id": r["data"]["id"], "relation": "different_conditions",
                         "resolution": "unresolved", "reason": "尚需双方独立材料。", "evidence": []}
                        for r in job["related"]],
        "follow_up": [],
    }


def payload_for(job, sources):
    return {**job, "sources": [source_excerpt(s, "", 10000) for s in sources]}


def test_review_preserves_locked_notes_and_expires_when_sources_change(role):
    saved, source = note(role)
    saved["data"]["locked"] = True
    role.store.write("knowledge", saved["data"], saved["version"], "user", "note")
    job = review_job(role)
    review = save_review(role, job, [source], payload_for(job, [source]), result_for(job, [source]))
    assert role.store.read("knowledge", "note")["data"] == saved["data"]
    assert latest_reviews(role.store, "123")[0]["note"] == review
    source["data"]["segments"][0]["text"] = "人工纠正实际材料"
    role.store.write("source_snapshots", source["data"], source["version"], "user", source["data"]["id"])
    assert latest_reviews(role.store, "123") == ({}, 1)
    assert annotations(role.store, [{"kind": "knowledge", **job["target"]}])[0]["review"] is None


def test_review_rejects_invented_quotes_partial_comparisons_and_missing_other_source(role):
    _, first = note(role)
    _, second = note(role, "other", "BV1xx411c7mE", "在另一个环境中应同时改变多个变量。")
    job = review_job(role)
    sources = [first, second]
    good = result_for(job, sources)
    invented = deepcopy(good)
    invented["claims"][0]["evidence"][0]["quote"] = "不存在的实验证据"
    with pytest.raises(AssistantError, match="逐字"):
        save_review(role, job, sources, payload_for(job, sources), invented)
    omitted = deepcopy(good)
    omitted["comparisons"] = []
    with pytest.raises(AssistantError, match="完整对应"):
        save_review(role, job, sources, payload_for(job, sources), omitted)
    premature = deepcopy(good)
    premature["comparisons"][0].update(relation="contradiction", resolution="prefer_target")
    premature["comparisons"][0]["evidence"] = deepcopy(good["claims"][0]["evidence"])
    with pytest.raises(AssistantError, match="双方材料"):
        save_review(role, job, sources, payload_for(job, sources), premature)
    premature["comparisons"][0]["evidence"] = [
        {"source_id": s["data"]["id"], "quote": s["data"]["segments"][0]["text"]} for s in sources
    ]
    assert save_review(role, job, sources, payload_for(job, sources), premature)
    assert len(role.store.records("knowledge")[0]) == 2


def test_review_compare_and_swap_and_same_video_different_page(role):
    saved, source = note(role)
    video = material()
    video["url"] += "&p=2"
    # Normalized page query prevents a different partition supplying evidence.
    video["url"] = video["url"].replace("?p=1&p=2", "?p=2")
    other = snapshot(role, video)
    assert source_for(role.store, saved, "123")["data"]["url"].endswith("?p=1")
    job = review_job(role)
    proposal = result_for(job, [source])
    proposal["claims"][0]["evidence"][0]["source_id"] = other["data"]["id"]
    with pytest.raises(AssistantError, match="该笔记"):
        save_review(role, job, [other], payload_for(job, [other]), proposal)
    saved["data"]["content"] = "用户修正笔记"
    role.store.write("knowledge", saved["data"], saved["version"], "user", "note")
    with pytest.raises(AssistantError, match="变化"):
        save_review(role, job, [source], payload_for(job, [source]), result_for(job, [source]))


@pytest.mark.asyncio
async def test_review_lane_refetches_source_respects_budget_and_refreshes_after_due(role, monkeypatch):
    configure(role, knowledge_review=True, memory_maintenance=False, review_daily_limit=1)
    role.adapter = LearningAdapter()
    note(role)
    scheduler = Scheduler(role, role.run()["generation"])
    async def model(prompt, payload, schema, images=()):
        sources = [role.store.read("source_snapshots", r["id"]) for r in payload["sources"]]
        return result_for(payload, sources)
    monkeypatch.setattr(scheduler, "model", model)
    assert (await scheduler.maintenance())["type"] == "knowledge_review"
    assert role.adapter.read_videos == ["BV1xx411c7mD"]
    assert await maintain(scheduler) is None
    before = role.clock()
    role.clock = lambda: before + timedelta(days=8)
    assert (await maintain(scheduler))["type"] == "knowledge_review"
    monkeypatch.setattr(scheduler, "remaining_model_calls", lambda: 0)
    assert await maintain(scheduler) is None


def skill(role):
    saved, _ = note(role)
    return role.remember("skills", {
        "schema_version": 1, "id": "method", "title": "最小案例", "content": "运行案例并改变一个参数。",
        "when_to_use": "调试", "limitations": "不能推广到所有问题", "knowledge_ids": [saved["data"]["id"]],
        "source": saved["data"]["source"], "tags": [], "locked": False,
    })


def test_method_reports_require_current_user_and_never_claim_independent_execution(role, admin):
    method = skill(role)
    initial = review_job(role)
    sources = [source_for(role.store, initial["target"], "123")]
    save_review(role, initial, sources, payload_for(initial, sources), result_for(initial, sources))
    assert review_job(role) is None
    quote = "我试了单参数方法，这次没找到原因。"
    current = append(role.store, "123", "codex", "role", "m1", quote, "user", "", "received",
                     role.clock().isoformat())[0]
    other = append(role.store, "123", "codex", "other", "m1", quote, "user", "", "received",
                   role.clock().isoformat())[0]
    args = dict(skill_id="method", mode="user_report", case="调试", expected="定位原因", observed="未找到",
                outcome="failed", limitations="单次反馈", evidence_id=current, evidence_quote=quote)
    with pytest.raises(AssistantError, match="本角色聊天"):
        record_trial(role, **(args | {"evidence_id": other}))
    with pytest.raises(AssistantError, match="role"):
        record_trial(admin, **args)
    saved = record_trial(role, **args)
    assert saved == record_trial(role, **args)
    status = trial_status(role.store, method, "123")
    assert status["level"] == "user_report" and status["requires_review"]
    assert not status["independently_executed"]
    assert review_job(role)["target"]["data"]["id"] == "note"
    assert review_job(role)["method_trials"][0]["data"]["id"] == saved["data"]["id"]
    turn = role.store.read("conversation_turns", current)
    turn["data"]["content"] = "我纠正上一条反馈"
    role.store.write("conversation_turns", turn["data"], turn["version"], "user", current)
    assert trial_status(role.store, method, "123")["level"] == "extracted_only"


@pytest.mark.asyncio
async def test_review_tools_read_only_for_admin_and_source_export(role, admin, tmp_path):
    import zipfile

    note(role)
    result = await call(admin, "knowledge_status", {})
    assert result["knowledge_count"] == 1
    path = tmp_path / "export.zip"
    role.store.export(path)
    with zipfile.ZipFile(path) as archive:
        assert any(p.startswith("source_snapshots/") for p in archive.namelist())
    assert ref(role.store.read("knowledge", "note"))["id"] == "note"
