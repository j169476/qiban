"""Run actual Bilibili services through both orchestration entry points, with fake I/O."""
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from conftest import FakeHost
from test_cognition import (
    BVID,
    DECISION_SCHEMA,
    PLAN_SCHEMA,
    LearningAdapter,
    configure,
    decision,
    material,
    signal,
)
from test_core_takeover import NOW, incoming
from test_learning import SearchAdapter, goal, model_stub
from test_native_tools import web_response
from test_video_progress import GapAdapter

from bili_assistant import native_tools, video_progress
from bili_assistant.connector import normalize
from bili_assistant.core_binding import companion
from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError
from bili_assistant.rich_content import private_content
from bili_assistant.scheduler import Scheduler
from bili_assistant.storage import Store
from companion_core import budget, events


def full_decision(**changes):
    return decision(native_request=None, video_gap=None, **changes)


class ScriptedModel:
    def __init__(self, response=None):
        self.calls, self.response = [], response

    async def generate(self, instructions, context, schema, images=(), **options):
        self.calls.append((deepcopy(context), schema, options))
        if self.response:
            return await self.response(instructions, context, schema, images, **options)
        if schema == PLAN_SCHEMA:
            return {"bvid": BVID, "reason": "包含控制变量示例", "memory_refs": []}
        assert schema == DECISION_SCHEMA
        if context["material"]["events"]:
            event = context["material"]["events"][0]
            return full_decision(memory="共同讨论最小案例的边界。", relationships=[{
                "event_id": event["id"], "delta": 1, "impression": "喜欢案例", "facts": ["讨论过控制变量"]}],
                action={"kind": event["kind"], "payload": {
                    "event_id": event["id"], "text": "先改变一个参数，再比较结果。", "bvid": "", "uid": "", "units": 1}})
        return full_decision(knowledge="先运行最小案例，再只改变一个参数观察差异。", tags=["调试"],
                             skill={"title": "控制变量", "content": "一次改变一个变量并观察差异。",
                                    "when_to_use": "定位问题", "limitations": "变量之间可能交互"})


def configured(store, platform):
    engine = Engine(store, FakeHost(), platform, lambda: NOW)
    document = store.read("companion")
    document["data"].update(daily_reflection=False, knowledge_review=False, media_fallback="off", search_expansion=False)
    store.write("companion", document["data"], document["version"], "test")
    return engine


@pytest.mark.asyncio
async def test_default_and_native_core_use_same_complete_services(store, tmp_path, monkeypatch):
    duplicate = Store(tmp_path / "legacy")
    duplicate.initialize()
    for kind in ("manifest", "policy", "companion"):
        old = duplicate.read(kind)
        duplicate.write(kind, store.read(kind)["data"], old["version"], "test")
    snapshots = []
    for current, native in ((duplicate, False), (store, True)):
        platform = LearningAdapter(candidates=[(BVID, "最小案例")])
        engine = configured(current, platform)
        model = ScriptedModel()
        if native:
            controller = companion(engine, model)
            controller.start()
            cycle = controller.cycle
        else:
            engine.start()
            controller = Scheduler(engine, engine.run()["generation"])

            async def fake_generate(self, *args, **kwargs):
                return await model.generate(*args, **kwargs)

            monkeypatch.setattr("companion_codex.model.CodexModel.generate", fake_generate)
            cycle = controller.cycle
        first = await cycle()
        assert [stage["name"] for stage in first["workflows"][0]["stages"]] == [
            "emotion_recovery", "delivery_recovery", "inbox", "selection_context", "native_tools", "video_gap",
            "goal_learning", "exploration", "decision_and_delivery", "reflection", "maintenance"]
        platform.events, platform.candidates = [incoming()], []
        await cycle()
        await cycle()
        assert len(platform.sent) == 1
        assert len(model.calls) == 3
        if native:
            normalized = normalize("123", incoming())
            assert current.state("core-processed:" + normalized.id) == events.get(current, normalized.id)["version"]
            assert await controller.poll() == []
            assert current.state("run") is None
        snapshots.append({
            "knowledge": [record["data"]["content"] for record in current.records("knowledge")[0]],
            "skills": [record["data"]["content"] for record in current.records("skills")[0]],
            "relationship": current.read("relationships", "789")["data"]["affinity"],
            "reply": platform.sent[0][1]["text"],
            "model_remaining": budget.remaining(current, current.read("policy")["data"], NOW),
        })
    assert snapshots[0] == snapshots[1]


@pytest.mark.asyncio
async def test_native_core_completes_explicit_goal_across_recreated_cycles(store):
    platform = SearchAdapter()
    engine = configured(store, platform)
    calls = []
    raw = model_stub(engine, calls)

    async def response(instructions, context, schema, images=(), **options):
        value = await raw(instructions, context, schema, images)
        if "video_gap" in schema["properties"]:
            value.setdefault("video_gap", None)
        return value

    model = ScriptedModel(response)
    core = companion(engine, model)
    core.start()
    bound = core.platforms.get("bilibili", "123").engine
    saved = goal(bound)
    for phase in ["search", "read", "quiz", "answer", "grade", "done"]:
        report = await companion(engine, model).cycle()
        assert store.read("learning_goals", saved["data"]["id"])["data"]["phase"] == phase
        assert "exploration" not in [item["name"] for item in report["workflows"][0]["stages"]]
    assert store.read("learning_goals", saved["data"]["id"])["data"]["status"] == "completed"
    assert len(calls) == 6
    assert len(platform.searches) == 1 and platform.read_videos == [BVID]
    assert not platform.sent and store.state("run") is None


@pytest.mark.asyncio
async def test_native_web_task_is_routed_through_core_provider(store):
    engine = configured(store, LearningAdapter())

    async def response(instructions, context, schema, images=(), **options):
        assert options["native"] == "web"
        options["guard"]()
        return web_response()

    model = ScriptedModel(response)
    core = companion(engine, model)
    core.start()
    bound = core.platforms.get("bilibili", "123").engine
    record = native_tools.create(bound, "web", "JSON 如何解码？")
    report = await core.cycle()
    assert report["workflows"][0]["stages"][-1]["name"] == "native_tools"
    assert store.read("native_jobs", record["data"]["id"])["data"]["status"] == "completed"
    assert len(store.records("knowledge")[0]) == 1 and len(model.calls) == 1


@pytest.mark.asyncio
async def test_gap_reading_runs_through_core_without_consuming_another_video(store):
    platform = GapAdapter()

    async def inbox(started_at):
        return []

    platform.inbox = inbox
    engine = configured(store, platform)

    async def response(instructions, context, schema, images=(), **options):
        return full_decision(knowledge="缺口阅读：" + context["material"]["video"]["segments"][0]["text"])

    model = ScriptedModel(response)
    core = companion(engine, model)
    core.start()
    bound = core.platforms.get("bilibili", "123").engine
    await bound.video_context(BVID)
    video_progress.request(bound, BVID, 600, 1800, "后半段的处理方式？")
    for _ in range(2):
        report = await core.cycle()
        assert report["workflows"][0]["stages"][-1]["name"] == "video_gap"
    assert video_progress.status(bound, BVID)["coverage"]["text_complete"]
    assert len(store.records("knowledge")[0]) == 2
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    assert not platform.sent


@pytest.mark.asyncio
async def test_workflow_deferral_is_shared_with_direct_core_inbox(store):
    platform = LearningAdapter(events=[incoming(native="first")])
    engine = configured(store, platform)
    model = ScriptedModel()
    core = companion(engine, model)
    core.start()
    await core.cycle()
    policy = store.read("policy")
    policy["data"]["min_action_interval_seconds"] = 300
    store.write("policy", policy["data"], policy["version"], "test")
    platform.events = [incoming(native="second")]
    await core.cycle()
    assert await core.poll() == []
    bound = core.platforms.get("bilibili", "123").engine
    core.clock = bound.clock = lambda: NOW + timedelta(minutes=6)
    await core.cycle()
    assert len(platform.sent) == 2
    assert await core.poll() == []


@pytest.mark.asyncio
async def test_default_collector_backlog_compares_actual_times_not_offset_strings(store, monkeypatch):
    first = incoming(native="negative-offset")
    first["received"] = "2026-10-22T01:01:00-05:00"  # 06:01 UTC, after the 06:00 UTC run start.
    older = incoming(native="positive-offset")
    older["received"] = "2026-10-22T13:59:00+08:00"  # 05:59 UTC, before the run start.
    platform = LearningAdapter(events=[older, first])
    engine = configured(store, platform)
    engine.start()
    await engine.inbox()
    platform.events = []  # Both events are now off the platform's visible page.
    scheduler = Scheduler(engine, engine.run()["generation"])
    scheduler.collector = object()
    model = ScriptedModel()

    async def fake_generate(self, *args, **kwargs):
        return await model.generate(*args, **kwargs)

    monkeypatch.setattr("companion_codex.model.CodexModel.generate", fake_generate)
    await scheduler.cycle()
    assert len(platform.sent) == 1
    assert platform.sent[0][1]["event_id"] == first["id"]
    assert store.state("processed:123:" + first["id"])
    assert not store.state("processed:123:" + older["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("native_core", [False, True])
@pytest.mark.parametrize("source_changed", [False, True])
async def test_real_shortlink_event_keeps_validated_page_through_rich_context(role, monkeypatch, native_core, source_changed):
    configure(role, guide_uid="789", daily_reflection=False, memory_maintenance=False, visuals_enabled=False)
    entry = {"id": "dm-guide", "kind": "dm_reply", "uid": "789", "received": role.clock().isoformat(),
             **private_content({"msg_type": 1, "content": {"content": "https://b23.tv/first"}})}
    resolved, seen = [], []

    class Adapter(LearningAdapter):
        async def comment_page(self, *args, **kwargs):
            pytest.fail("A private message must not fetch a comment page")

        async def video_context(self, bvid, page_index=0):
            self.read_videos.append((bvid, page_index))
            value = material(bvid)
            value.update(page_index=page_index, cid="2", url=f"https://www.bilibili.com/video/{bvid}?p=2")
            if source_changed:
                changed = {**entry, "text": f"https://www.bilibili.com/video/{BVID}?p=4"}
                with role.store.db() as db:
                    db.execute("UPDATE events SET body=? WHERE id=?", (json.dumps(changed), entry["id"]))
            return value

    async def resolve_short(url, guard):
        guard()
        resolved.append(url)
        return {"bvid": BVID, "page_index": 1, "source_url": url,
                "resolved_url": f"https://www.bilibili.com/video/{BVID}?p=2"}

    monkeypatch.setattr("bili_assistant.content_refs.resolve_short", resolve_short)
    role.adapter = Adapter([entry])

    async def model(instructions, context, schema, images=(), **options):
        event = context["material"]["events"][0]
        assert event["bvid"] == BVID
        assert event["page_index"] == context["material"]["video"]["page_index"] == 1
        assert event["text"] == entry["text"] and event["attachments"] == entry["attachments"]
        seen.append(event)
        return full_decision(interest_signals=[signal(event["id"])])

    if native_core:
        role.stop(paused=True)
        controller = companion(role, ScriptedModel(model))
        controller.start()
    else:
        controller = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(controller, "model", model)
    if source_changed:
        with pytest.raises(AssistantError) as error:
            await controller.cycle()
        assert error.value.code == "context_changed"
        assert not seen and not role.store.records("interest_signals")[0]
    else:
        await controller.cycle()
        assert len(seen) == 1 and resolved == ["https://b23.tv/first"]
        assert role.store.records("interest_signals")[0][0]["data"]["bvid"] == BVID
        assert role.store.state("processed:123:" + entry["id"])
        with role.store.db() as db:
            assert json.loads(db.execute("SELECT body FROM events WHERE id=?", (entry["id"],)).fetchone()[0]) == entry
    assert not role.adapter.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("native_core", [False, True])
@pytest.mark.parametrize("changed", [None, "persona", "memories", "growth", "relationships"])
async def test_final_dispatch_freezes_context_but_allows_own_learning_updates(store, memory, monkeypatch, native_core, changed):
    store.write("memories", {**memory, "content": "讨论草图时采用分层结构。"}, None, "test", memory["id"])
    store.write("relationships", {"schema_version": 1, "id": "789", "display_name": "共同讨论者",
                                 "affinity": 50, "interactions": 0, "events": [], "locked": False}, None, "test", "789")

    class EditingAdapter(LearningAdapter):
        async def perform(self, kind, payload, policy, before_send):
            if changed:
                item_id = memory["id"] if changed == "memories" else "789" if changed == "relationships" else None
                record = store.read(changed, item_id)
                field = {"persona": "name", "memories": "content", "growth": "mood", "relationships": "impression"}[changed]
                record["data"][field] = "人工更正：旧上下文不可再采用"
                store.write(changed, record["data"], record["version"], "manual-edit", item_id)
            return await super().perform(kind, payload, policy, before_send)

    platform = EditingAdapter(events=[incoming()])
    engine = configured(store, platform)
    configure(engine, guide_uid="789")

    async def response(instructions, context, schema, images=(), **options):
        event = context["material"]["events"][0]
        assert any(record["data"]["id"] == memory["id"] for record in context["memory"]["records"])
        return full_decision(
            relationships=[{"event_id": event["id"], "delta": 1, "impression": "共同讨论草图", "facts": []}],
            emotion={"label": "好奇", "reason": "继续讨论草图细节", "valence_delta": 0.1, "arousal_delta": 0.1,
                     "evidence_ids": [event["id"]]},
            interest_signals=[signal(event["id"])],
            action={"kind": event["kind"], "payload": {"event_id": event["id"], "text": "可以按层拆开讨论。",
                                                       "bvid": "", "uid": "", "units": 1}},
        )

    model = ScriptedModel(response)
    if native_core:
        controller = companion(engine, model)
        controller.start()
    else:
        engine.start()
        controller = Scheduler(engine, engine.run()["generation"])
        monkeypatch.setattr(controller, "model", model.generate)
    await controller.cycle()
    assert len(model.calls) == 1
    assert len(platform.sent) == (0 if changed else 1)
    assert store.read("relationships", "789")["data"]["interactions"] == 1
    assert len(store.records("emotion_events")[0]) == len(store.records("interest_signals")[0]) == 1
    with store.db() as db:
        assert db.execute("SELECT status FROM actions").fetchone()[0] == ("cancelled" if changed else "succeeded")
    if changed:
        assert not store.state("processed:123:" + incoming()["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("native_core", [False, True])
async def test_model_packet_uses_active_controller_readonly_authority(store, monkeypatch, native_core):
    platform = LearningAdapter(candidates=[(BVID, "最小案例")])
    engine = configured(store, platform)
    model = ScriptedModel()
    if native_core:
        # A paused legacy snapshot with read_only=False must not override core.
        engine.start(read_only=False)
        engine.stop(paused=True)
        controller = companion(engine, model)
        controller.start(read_only=True)
    else:
        engine.start(read_only=True)
        controller = Scheduler(engine, engine.run()["generation"])
        monkeypatch.setattr(controller, "model", model.generate)
    await controller.cycle()
    assert model.calls and all(call[0]["runtime"]["read_only"] for call in model.calls)
    assert not platform.sent
