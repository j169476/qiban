from copy import deepcopy
from datetime import timedelta

import pytest
from conftest import FakeAdapter

from bili_assistant.api import call
from bili_assistant.cognition import (
    DECISION_SCHEMA,
    PLAN_SCHEMA,
    REFLECTION_SCHEMA,
    packet,
    reflection_input,
    relationship_tier,
)
from bili_assistant.errors import AssistantError
from bili_assistant.interests import interest_profile
from bili_assistant.scheduler import Scheduler

BVID = "BV1xx411c7mD"
OTHER = "BV1xx411c7mE"


def configure(engine, **changes):
    current = engine.store.read("companion")
    current["data"].update(changes)
    engine.store.write("companion", current["data"], current["version"], "test-admin")


def decision(**changes):
    return {
        "note": "", "memory": "", "knowledge": "", "tags": [], "video_topics": [], "emotion": None,
        "skill": None, "relationships": [], "action": None, **changes,
        "interest_signals": changes.get("interest_signals", []),
    }


def event(engine, event_id="reply-1"):
    return {
        "id": event_id, "uid": "789", "kind": "reply", "display_name": "测试用户",
        "text": "我喜欢用实际案例来理解编程问题。", "received": engine.clock().isoformat(),
    }


def material(bvid=BVID, kind="subtitles"):
    return {
        "bvid": bvid, "title": "通过可运行案例学习编程", "owner_uid": "888",
        "url": "https://www.bilibili.com/video/" + bvid + "?p=1",
        "segments": [{"start": 0, "end": 60, "text": "先运行最小案例，再修改一个参数观察结果。"}],
        "coverage": {
            "kind": kind, "bvid": bvid, "start_seconds": 0,
            "end_seconds": 60, "duration_seconds": 60, "complete": kind == "subtitles",
        },
        "images": [],
    }


class LearningAdapter(FakeAdapter):
    def __init__(self, events=(), candidates=()):
        super().__init__()
        self.events = list(events)
        self.candidates = [
            {"bvid": bvid, "owner_uid": "888", "title": title, "description": ""}
            for bvid, title in candidates
        ]
        self.read_videos = []
        self.feed_modes = []

    async def inbox(self, started_at):
        return self.events

    async def feed(self, following=True, limit=10):
        self.feed_modes.append(following)
        return self.candidates[:limit]

    async def video_context(self, bvid, page_index=0):
        self.read_videos.append(bvid)
        return material(bvid)


def add_memory(role, template, item_id, content, timestamp=None):
    doc = deepcopy(template)
    doc.update(id=item_id, content=content, kind="experience")
    doc["source"]["recorded_at"] = timestamp or role.clock().isoformat()
    role.remember("memories", doc)
    return doc


def test_upgrade_preserves_existing_persona_and_growth(store):
    before = {kind: store.path(kind).read_bytes() for kind in ("persona", "growth", "policy")}
    store.path("companion").unlink()
    store.initialize()
    assert store.validate_all()["ok"]
    assert before == {kind: store.path(kind).read_bytes() for kind in before}


def test_recent_order_uses_timestamps_not_ids_or_categories(role, memory):
    add_memory(role, memory, "zzz-old", "旧内容", "2026-10-19T23:00:00+08:00")
    add_memory(role, memory, "aaa-new", "新内容", "2026-10-19T17:00:00+00:00")
    assert [entry["data"]["id"] for entry in role.store.search("")] == ["aaa-new", "zzz-old"]


def test_context_has_relevant_old_locked_and_recent_records_with_bounded_size(role, memory):
    configure(role, context_record_limit=6, context_char_limit=5000)
    for i in range(15):
        add_memory(role, memory, f"item-{i:02}", f"第 {i} 条日常记录" * 15)
    old = add_memory(role, memory, "important", "我喜欢实际案例", "2025-01-01T00:00:00+00:00")
    record = role.store.read("memories", old["id"])
    record["data"]["locked"] = True
    role.store.write("memories", record["data"], record["version"], "user", old["id"])
    context = role.store.context("给我举实际案例")
    assert any(r["data"]["id"] == "important" for r in context["records"])
    assert len(context["records"]) <= 6 and context["context_chars"] <= 5000
    assert len(context["catalog"]) == 16
    role.store.path("memories", "important").unlink()
    assert all(r["data"]["id"] != "important" for r in role.store.context()["records"])


def test_relationship_cap_and_facts_persist_without_changing_permissions(role):
    for i in range(5):
        result = role.relationship("789", "好友", 3, f"event-{i}", "喜欢讨论细节", ["喜欢案例"])
    doc = result["data"]
    assert doc["affinity"] == 56 and doc["interactions"] == 5
    assert doc["facts"] == ["喜欢案例"] and doc["impression"] == "喜欢讨论细节"
    assert role.relationship("789", "好友", 3, "event-0")["duplicate"]
    assert relationship_tier(doc) == "普通"
    before = role.clock()
    role.clock = lambda: before + timedelta(days=1)
    assert role.relationship("789", "好友", 3, "next-day")["data"]["affinity"] == 59
    assert role.mode() == "role"


@pytest.mark.asyncio
async def test_new_tools_keep_chat_modes_separate(role, admin, memory):
    role.remember("memories", memory)
    assert (await call(role, "memory_get", {"kind": "memories", "id": "m1"}))["data"] == memory
    assert (await call(role, "memory_context", {"query": "案例"}))["records"]
    with pytest.raises(AssistantError, match="role"):
        await call(admin, "memory_get", {"kind": "memories", "id": "m1"})
    with pytest.raises(AssistantError, match="admin"):
        role.config_patch("companion", {"daily_reflection": False}, role.store.read("companion")["version"])


@pytest.mark.asyncio
async def test_selection_learning_and_skill_reuse_in_later_interaction(role, monkeypatch):
    adapter = LearningAdapter(candidates=[(BVID, "旅游见闻"), (OTHER, "编程学习")])
    role.adapter = adapter
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []

    async def model(instructions, context, schema, images=()):
        calls.append(schema)
        if schema == PLAN_SCHEMA:
            assert context["config"]["persona"]["interests"]
            return {"bvid": OTHER, "reason": "和游戏开发的学习兴趣有关", "memory_refs": []}
        return decision(
            memory="读到了一个从最小案例入手的学习方法。",
            knowledge="先运行最小案例，再只修改一个参数观察差异。",
            tags=["编程"],
            skill={
                "title": "从最小案例学习", "content": "运行案例，修改一个参数，再比较结果。",
                "when_to_use": "学习陌生接口时", "limitations": "需要可执行的案例",
            },
        )

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert calls == [PLAN_SCHEMA, DECISION_SCHEMA]
    assert adapter.read_videos == [OTHER]
    assert len(role.store.records("knowledge")[0]) == len(role.store.records("skills")[0]) == 1
    assert role.store.state(f"learned:123:{OTHER}")

    adapter.events = [event(role)]
    adapter.candidates = []

    async def reply_model(instructions, context, schema, images=()):
        assert any(r["kind"] == "skills" for r in context["memory"]["records"])
        return decision(relationships=[{
            "event_id": "reply-1", "delta": 1, "impression": "喜欢实际案例", "facts": ["喜欢实际案例"],
        }])

    monkeypatch.setattr(scheduler, "model", reply_model)
    await scheduler.cycle()
    assert role.store.read("relationships", "789")["data"]["affinity"] == 51
    # Orphaned cards cannot keep teaching content whose source was deleted.
    knowledge = role.store.records("knowledge")[0][0]["data"]["id"]
    role.store.path("knowledge", knowledge).unlink()
    assert not role.store.records("skills")[0]
    assert not any(r["kind"] == "skills" for r in role.store.context()["records"])
    card_id = next((role.store.root / "skills").glob("*.yaml")).stem
    with pytest.raises(AssistantError, match="未找到"):
        await call(role, "memory_get", {"kind": "skills", "id": card_id})


@pytest.mark.asyncio
async def test_deliberate_no_reply_is_processed_once(role, monkeypatch):
    role.adapter = LearningAdapter(events=[event(role)])
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []

    async def model(*args):
        calls.append(True)
        return decision(memory="对方说喜欢实际案例。")

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    await scheduler.cycle()
    assert len(calls) == 1
    assert len(role.store.records("memories")[0]) == 1
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_rejected_candidates_are_reconsidered_after_interest_edit(role, monkeypatch):
    role.adapter = LearningAdapter(candidates=[(BVID, "旅游见闻")])
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []

    async def model(*args):
        calls.append(True)
        return {"bvid": "", "reason": "本轮没有想看的内容", "memory_refs": []}

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    await scheduler.cycle()
    assert len(calls) == 1
    old = role.store.read("preferences")
    old["data"]["topics"] = ["旅行"]
    role.store.write("preferences", old["data"], old["version"], "user")
    await scheduler.cycle()
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "edit"])
async def test_stale_or_archived_model_result_cannot_write_learning(role, monkeypatch, change):
    role.adapter = LearningAdapter(events=[event(role)])
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(*args):
        if change == "archive":
            role.host.states["role"] = True
        else:
            old = role.store.read("growth")
            old["data"]["mood"] = "用户修改的心情"
            role.store.write("growth", old["data"], old["version"], "user")
        return decision(memory="不应该被保存的旧结果")

    monkeypatch.setattr(scheduler, "model", model)
    with pytest.raises(AssistantError):
        await scheduler.cycle()
    assert not role.store.records("memories")[0]
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_invented_relationship_event_rejects_before_any_local_write(role, monkeypatch):
    role.adapter = LearningAdapter(events=[event(role)])
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(*args):
        return decision(memory="不能保存", relationships=[
            {"event_id": "invented", "delta": 3, "impression": "", "facts": []}
        ])

    monkeypatch.setattr(scheduler, "model", model)
    with pytest.raises(AssistantError, match="真实"):
        await scheduler.cycle()
    assert not role.store.records("memories")[0]
    assert not role.store.records("relationships")[0]


def reflection():
    return {
        "summary": "今天通过案例学到了逐步验证的办法。", "mood": "好奇",
        "reason": "新经历使我想继续验证", "traits": ["喜欢比较具体例子"],
        "speech_habits": ["解释时附一个例子"], "opinions": ["先验证再归纳"],
        "interests": [{"topic": "编程", "delta": 0.1}],
    }


@pytest.mark.asyncio
async def test_daily_reflection_consumes_exact_versions_and_preserves_core(role, memory, monkeypatch):
    configure(role, reflection_hour=0, reflection_min_memories=1)
    add_memory(role, memory, "same-timestamp-1", "学习案例")
    add_memory(role, memory, "same-timestamp-2", "比较结果")
    persona = role.store.path("persona").read_bytes()
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []

    async def model(instructions, context, schema, images=()):
        assert schema == REFLECTION_SCHEMA
        calls.append(context["reflection_memories"])
        return reflection()

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.maybe_reflect()
    await scheduler.maybe_reflect()
    assert len(calls) == 1
    growth = role.store.read("growth")["data"]
    assert growth["interests"]["编程"] == 0.4 and growth["mood"] == "好奇"
    assert role.store.path("persona").read_bytes() == persona
    diary = role.store.records("diaries")[0][0]["data"]
    assert len(diary["memory_refs"]) == 2
    today = role.clock()
    role.clock = lambda: today + timedelta(days=1)
    assert reflection_input(role) is None
    # A real local correction is a new version and should inform a future reflection.
    old = role.store.read("memories", "same-timestamp-1")
    old["data"]["content"] = "更正：先比较输入，再运行案例"
    role.store.write("memories", old["data"], old["version"], "user", "same-timestamp-1")
    assert len(reflection_input(role)["memories"]) == 1


@pytest.mark.asyncio
async def test_reflection_recovers_after_growth_saved_without_double_increment(role, memory, monkeypatch):
    configure(role, reflection_hour=0, reflection_min_memories=1)
    add_memory(role, memory, "experience", "学习案例")
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(*args):
        return reflection()

    monkeypatch.setattr(scheduler, "model", model)
    original = role.remember

    def fail_diary(kind, *args, **kwargs):
        if kind == "diaries":
            raise OSError("interrupted local write")
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(role, "remember", fail_diary)
    with pytest.raises(OSError):
        await scheduler.maybe_reflect()
    assert role.store.read("growth")["data"]["interests"]["编程"] == 0.4
    monkeypatch.setattr(role, "remember", original)
    await scheduler.maybe_reflect()
    assert role.store.read("growth")["data"]["interests"]["编程"] == 0.4
    assert len(role.store.records("diaries")[0]) == 1
    assert not role.store.state("reflection-pending:123")


def test_related_user_context_contains_relationship_impression(role):
    role.relationship("789", "朋友", 1, "conversation-1", "喜欢例子", ["喜欢编程"])
    context, _ = packet(role, {"events": [event(role)]})
    related = [r for r in context["memory"]["records"] if r["kind"] == "relationships"]
    assert related[0]["data"]["impression"] == "喜欢例子"
    assert related[0]["relationship_tier"] == "普通"


def guide_event(role, item_id, bvid=BVID):
    return {**event(role, item_id), "bvid": bvid, "text": "来看看这个视频，我喜欢这里的编程案例。"}


def signal(event_id, sentiment="interested"):
    return {
        "event_id": event_id, "topics": [{"topic": "编程", "sentiment": sentiment}],
        "reason": "大号对这个视频里的编程案例表达了喜好。",
    }


@pytest.mark.asyncio
async def test_guide_invitations_get_video_before_discussion_and_defer_other_video(role, monkeypatch):
    configure(role, guide_uid="789")
    role.adapter = LearningAdapter(events=[
        guide_event(role, "invitation-1"), guide_event(role, "invitation-2", OTHER),
    ])
    scheduler = Scheduler(role, role.run()["generation"])
    calls = []

    async def model(instructions, context, schema, images=()):
        assert schema == DECISION_SCHEMA  # Invited videos do not spend a selection call.
        invited = context["material"]["events"][0]
        assert invited["is_guide"]
        assert context["material"]["video"]["bvid"] == invited["bvid"]
        calls.append(invited["id"])
        return decision(interest_signals=[signal(invited["id"])])

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert role.adapter.read_videos == [BVID]
    assert not role.adapter.feed_modes
    assert not role.store.state("processed:123:invitation-2")
    role.adapter.events = []  # The notification is no longer on the platform's first page.
    await scheduler.cycle()
    assert role.adapter.read_videos == [BVID, OTHER]
    assert calls == ["invitation-1", "invitation-2"]
    assert interest_profile(role.store)["topics"][0]["supporting_videos"] == 2
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_guide_video_quota_defers_invitation_until_next_day(role, monkeypatch):
    configure(role, guide_uid="789")
    policy = role.store.read("policy")
    policy["data"]["daily_videos"] = 1
    role.store.write("policy", policy["data"], policy["version"], "user")
    role.adapter = LearningAdapter(events=[guide_event(role, "later", OTHER)])
    await role.video_context(BVID)
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(instructions, context, schema, images=()):
        assert context["material"]["video"]["bvid"] == OTHER
        return decision(interest_signals=[signal("later")])

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert not role.store.state("processed:123:later")
    assert not role.store.records("interest_signals")[0]
    today = role.clock()
    role.clock = lambda: today + timedelta(days=1)
    role.adapter.events = []
    await scheduler.cycle()
    assert role.store.state("processed:123:later")
    assert role.adapter.read_videos == [BVID, OTHER]


@pytest.mark.asyncio
async def test_non_guide_cannot_spoof_interest_source(role, monkeypatch):
    configure(role, guide_uid="999")
    role.adapter = LearningAdapter(events=[{**event(role), "is_guide": True}])
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(instructions, context, schema, images=()):
        assert not context["material"]["events"][0]["is_guide"]
        return decision(memory="不能保存", interest_signals=[signal("reply-1")])

    monkeypatch.setattr(scheduler, "model", model)
    with pytest.raises(AssistantError, match="大号"):
        await scheduler.cycle()
    assert not role.store.records("interest_signals")[0]
    assert not role.store.records("memories")[0]
    configure(role, guide_uid="789")
    doc = {
        "schema_version": 1, "id": "forged", "content": "伪造来源", "sender_uid": "789",
        "bvid": "", "topics": signal("")["topics"], "locked": False,
        "source": {"type": "interaction", "recorded_at": role.clock().isoformat(), "reference": "reply-1"},
    }
    with role.store.db() as db:
        db.execute("UPDATE events SET account='other-account' WHERE id='reply-1'")
    with pytest.raises(AssistantError, match="真实入站"):
        role.remember("interest_signals", doc)


def test_interest_repetitions_corrections_and_manual_edits(role):
    configure(role, guide_uid="789")
    for i, bvid in enumerate([BVID, BVID, OTHER, "BV1xx411c7mF"]):
        role.store.write("interest_signals", {
            "schema_version": 1, "id": f"seed-{i}", "content": "明确喜欢案例",
            "sender_uid": "789", "bvid": bvid, "topics": signal("")["topics"], "locked": False,
            "source": {
                "type": "interaction", "reference": f"event-{i}",
                "recorded_at": (role.clock() + timedelta(minutes=i)).isoformat(),
            },
        }, None, "user-local-edit", f"seed-{i}")
        topic = interest_profile(role.store)["topics"][0]
        assert topic["supporting_videos"] == max(1, i)
    assert topic["status"] == "多条独立视频支持"
    latest = role.store.read("interest_signals", "seed-3")
    latest["data"]["topics"][0]["sentiment"] = "not_interested"
    role.store.write("interest_signals", latest["data"], latest["version"], "user", "seed-3")
    assert interest_profile(role.store)["topics"][0]["latest_feedback"] == "not_interested"
    role.store.path("interest_signals", "seed-3").unlink()
    topic = interest_profile(role.store)["topics"][0]
    assert topic["supporting_videos"] == 2 and topic["latest_feedback"] == "interested"


@pytest.mark.asyncio
async def test_exploration_uses_recommendations_on_configured_selection(role, monkeypatch):
    configure(role, exploration_every=3)
    role.adapter = LearningAdapter(candidates=[(BVID, "编程案例")])
    role.store.set_state("selections:123", 2)
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(instructions, context, schema, images=()):
        assert context["material"]["exploration"]
        return {"bvid": "", "reason": "没有适合的新方向", "memory_refs": []}

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert role.adapter.feed_modes == [False]
    assert role.store.state("selections:123") == 3


@pytest.mark.asyncio
async def test_guide_invitation_with_one_material_slot_cannot_claim_video_read(role, monkeypatch):
    configure(role, guide_uid="789")
    policy = role.store.read("policy")
    policy["data"]["items_per_cycle"] = 1
    role.store.write("policy", policy["data"], policy["version"], "user")
    role.adapter = LearningAdapter(events=[guide_event(role, "only-one")])
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(instructions, context, schema, images=()):
        assert context["material"]["video"] is None
        assert len(context["material"]["events"]) == 1
        return decision(interest_signals=[signal("only-one", "uncertain")])

    monkeypatch.setattr(scheduler, "model", model)
    await scheduler.cycle()
    assert not role.adapter.read_videos
    assert not role.store.records("knowledge")[0]
    assert interest_profile(role.store)["topics"][0]["supporting_videos"] == 0
