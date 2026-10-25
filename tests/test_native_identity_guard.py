"""A deferred native job keeps the identity evidence behind its original decision."""
import time
from copy import deepcopy

import pytest
from PIL import Image
from test_identity_decisions import answer, linked, scheduler_for
from test_native_tools import NativeScheduler, web_response

from bili_assistant import native_tools as native
from bili_assistant.cognition import save_learning
from companion_core.errors import AssistantError


async def decision_for(role, kind="image"):
    policy = role.store.read("policy")
    policy["data"]["actions"]["dynamic"] = {"enabled": True, "daily_limit": 2}
    role.store.write("policy", policy["data"], policy["version"], "test")
    incoming, _, _, service, hard = linked(role)
    result = answer(incoming)
    result["action"] = {"kind": "dynamic", "payload": {
        "bvid": "", "text": "我们和这两个账号一起聊过蓝色桌面的草图。", "event_id": "", "uid": "", "units": 1,
    }} if kind == "image" else None
    result["native_request"] = {"kind": kind, "prompt": "蓝色桌面草图", "use_persona": False}
    result["relationships"] = [{"event_id": incoming["id"], "delta": 1, "impression": "讨论草图", "facts": []}]
    material = {"events": [incoming], "video": None}
    decision = await scheduler_for(role, incoming, result).decide(material)
    save_learning(role, material, decision)
    return material, decision, lambda: service.revoke(hard["data"]["id"], hard["version"], "管理端撤销错误关联")


def generated_image(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    thread = "11111111-1111-1111-1111-111111111111"
    image = tmp_path / "generated_images" / thread / "test.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(image)
    return {"result": {"status": "done", "reason": "", "image_path": str(image), "sources": []},
            "evidence": {"thread_id": thread, "started": time.time(), "opened": []}}


def saved_job(role):
    return role.store.records("native_jobs")[0][0]


@pytest.mark.asyncio
async def test_stale_post_save_decision_cannot_create_native_job(role):
    material, decision, revoke = await decision_for(role)
    revoke()
    with pytest.raises(AssistantError) as error:
        native.from_decision(role, material, decision)
    assert error.value.code == "identity_context_changed"
    assert role.store.records("native_jobs")[0] == []


@pytest.mark.asyncio
async def test_queued_job_retains_frozen_dependencies_and_stops_before_model_on_revoke(role):
    material, decision, revoke = await decision_for(role)
    dependencies = deepcopy(decision["_identity_after_save"])
    assert dependencies
    native.from_decision(role, material, decision)
    assert saved_job(role)["data"]["identity_dependencies"] == dependencies
    decision["_identity_after_save"].clear()
    assert saved_job(role)["data"]["identity_dependencies"] == dependencies
    revoke()
    scheduler = NativeScheduler(role, {})
    assert await native.step(scheduler)
    doc = saved_job(role)["data"]
    assert doc["status"] == "cancelled" and doc["error"] == "identity_context_changed"
    assert not scheduler.calls and not role.adapter.sent
    assert native.status(role)["used_today"]["image"] == 0
    with pytest.raises(AssistantError) as error:
        native.control(role, doc["id"], "retry", saved_job(role)["version"])
    assert error.value.code == "identity_context_changed"


@pytest.mark.parametrize("stage", [None, "generation", "review", "before_send"])
@pytest.mark.asyncio
async def test_image_job_checks_identity_at_generation_review_and_platform_send(role, tmp_path, monkeypatch, stage):
    material, decision, revoke = await decision_for(role)
    native.from_decision(role, material, decision)
    scheduler = NativeScheduler(role, generated_image(tmp_path, monkeypatch))

    def change():
        if stage == "generation" and len(scheduler.calls) == 1 or stage == "review" and len(scheduler.calls) == 2:
            revoke()

    scheduler.callback = change
    if stage == "before_send":
        perform = role.adapter.perform

        async def revoke_before_send(kind, payload, policy, before_send):
            revoke()
            return await perform(kind, payload, policy, before_send)

        role.adapter.perform = revoke_before_send
    assert await native.step(scheduler)
    doc = saved_job(role)["data"]
    assert doc["status"] == ("cancelled" if stage else "completed"), doc
    assert doc["error"] == ("identity_context_changed" if stage else "")
    assert len(role.adapter.sent) == (0 if stage else 1)
    if stage == "generation":
        assert role.store.records("publication_images")[0] == []
    if stage == "before_send":
        with role.store.db() as db:
            assert db.execute("SELECT status FROM actions").fetchone()[0] == "cancelled"
    assert not await native.step(scheduler)


@pytest.mark.asyncio
async def test_web_job_does_not_save_notes_from_revoked_identity_context(role):
    material, decision, revoke = await decision_for(role, "web")
    native.from_decision(role, material, decision)
    assert await native.step(NativeScheduler(role, web_response(), revoke))
    assert saved_job(role)["data"]["error"] == "identity_context_changed"
    assert role.store.records("source_snapshots")[0] == []
    assert role.store.records("knowledge")[0] == []


@pytest.mark.asyncio
async def test_existing_pending_job_cannot_bypass_new_decision_provenance(role):
    material, decision, revoke = await decision_for(role)
    request = decision["native_request"]
    original = native.create(role, "image", request["prompt"], draft=decision["action"]["payload"]["text"])
    assert original["data"]["identity_dependencies"] == []
    native.from_decision(role, material, decision)
    assert saved_job(role)["data"]["identity_dependencies"] == decision["_identity_after_save"]
    revoke()
    await native.step(NativeScheduler(role, {}))
    assert saved_job(role)["data"]["status"] == "cancelled" and not role.adapter.sent


@pytest.mark.asyncio
async def test_legacy_job_without_identity_field_remains_compatible(role):
    record = native.create(role, "web", "JSON 如何解码？")
    record["data"].pop("identity_dependencies")
    role.store.write("native_jobs", record["data"], record["version"], "test", record["data"]["id"])
    assert await native.step(NativeScheduler(role, web_response()))
    assert saved_job(role)["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_uncertain_publication_keeps_receipt_and_never_retries_after_identity_revoke(role, tmp_path, monkeypatch):
    material, decision, revoke = await decision_for(role)
    native.from_decision(role, material, decision)
    role.adapter.fail = True
    scheduler = NativeScheduler(role, generated_image(tmp_path, monkeypatch))
    assert await native.step(scheduler)
    doc = saved_job(role)["data"]
    assert doc["status"] == "failed" and doc["result"]["publication"] == "uncertain"
    assert doc["result"]["action_id"] and len(role.adapter.sent) == 1
    revoke()
    assert not await native.step(scheduler)
    with pytest.raises(AssistantError) as error:
        native.control(role, doc["id"], "retry", saved_job(role)["version"])
    assert error.value.code == "native_retry"
    assert len(role.adapter.sent) == 1
