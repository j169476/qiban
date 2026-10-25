from copy import deepcopy

import pytest

from bili_assistant.cognition import save_learning
from bili_assistant.connector import normalize
from bili_assistant.scheduler import Scheduler
from companion_core import events
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.identity import IdentityService
from companion_core.people import bind_relationship


def linked(role):
    incoming = {"id": "dm-789-identity", "kind": "dm_reply", "uid": "789", "display_name": "测试用户",
                "received": role.clock().isoformat(), "text": "我的QQ号是7654321，昨天约好讨论蓝色桌面的草图。"}
    first = normalize("123", incoming)
    other = Event("qq", "bot", "other-proof", ActorRef("qq", "7654321"), "dm:7654321",
                  role.clock().isoformat(), "我是昨天和你约好讨论蓝色桌面草图的那个人。")
    refs = []
    for item in (first, other):
        refs.append({"event_id": item.id, "version": events.ingest(role.store, item)["version"], "quote": item.text})
    service = IdentityService(role.store)
    common = {"left": first.actor.as_dict(), "right": other.actor.as_dict(), "reason": "双边对话明确对应同一个具体约定"}
    service.apply({**common, "kind": "claim", "specific_correspondence": False, "evidence": refs[:1]})
    record = service.apply({**common, "kind": "correspondence", "specific_correspondence": True, "evidence": refs})
    return incoming, first, other, service, record


def answer(incoming):
    return {"note": "", "memory": "两个账号对应同一个人的蓝色桌面讨论。", "knowledge": "", "tags": [],
            "video_topics": [], "emotion": None, "skill": None, "relationships": [], "identity_proposals": [],
            "interest_signals": [], "native_request": None, "video_gap": None,
            "action": {"kind": "dm_reply", "payload": {"bvid": "", "text": "我们接着讨论蓝色桌面的草图。",
                       "event_id": incoming["id"], "uid": "", "units": 1}}}


def scheduler_for(role, incoming, result, change=lambda: None):
    class Inbox(type(role.adapter)):
        async def inbox(self, started_at):
            return [incoming]

        async def feed(self, **kwargs):
            return []

    role.adapter = Inbox()
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(instructions, context, schema, images=(), **kwargs):
        if "只评估跨平台身份" in instructions:
            return {"identity_proposals": []}
        assert context["identity_people"][0]["person"]["inferred"]
        change()
        return deepcopy(result)

    async def noop():
        pass

    scheduler.model = model
    scheduler.maintenance = scheduler.maybe_reflect = noop
    return scheduler


@pytest.mark.parametrize("change_kind", ["revoke", "evidence_deleted", "shared_impression"])
async def test_bili_cycle_discards_decision_when_identity_dependency_changes(role, change_kind):
    incoming, _, other, service, hard = linked(role)
    if change_kind == "shared_impression":
        role.relationship("7654321", "共享人物资料", 0, "seed", "原始印象")
        bind_relationship(role.store, other.actor, "7654321")
    def change():
        if change_kind == "revoke":
            service.revoke(hard["data"]["id"], hard["version"], "人工确认关联对象并非同一个人")
        elif change_kind == "evidence_deleted":
            events.forget(role.store, other.id)
        else:
            old = role.store.read("relationships", "7654321")
            old["data"]["impression"] = "用户在模型调用期间修正过的印象"
            role.store.write("relationships", old["data"], old["version"], "admin", "7654321")
    scheduler = scheduler_for(role, incoming, answer(incoming), change)
    with pytest.raises(AssistantError) as exc:
        await scheduler.cycle()
    assert exc.value.code == "context_changed"
    assert not role.adapter.sent
    assert not role.store.records("memories")[0]


async def test_bili_rechecks_snapshot_at_persistence_boundary(role):
    incoming, _, _, service, hard = linked(role)
    scheduler = scheduler_for(role, incoming, answer(incoming))
    material = {"events": [incoming], "video": None}
    decision = await scheduler.decide(material)
    service.revoke(hard["data"]["id"], hard["version"], "模型完成后保存前撤销错误关联")
    with pytest.raises(AssistantError) as exc:
        save_learning(role, material, decision)
    assert exc.value.code == "context_changed"
    assert not role.store.records("memories")[0]


async def test_bili_rechecks_identity_at_actual_send_boundary(role):
    incoming, _, _, service, hard = linked(role)
    scheduler = scheduler_for(role, incoming, answer(incoming))
    original = role.adapter.perform
    async def revoke_before_send(kind, payload, policy, before_send):
        service.revoke(hard["data"]["id"], hard["version"], "派发前人工撤销了错误关联")
        return await original(kind, payload, policy, before_send)
    role.adapter.perform = revoke_before_send
    await scheduler.cycle()
    assert not role.adapter.sent
    with role.store.db() as db:
        assert db.execute("SELECT status FROM actions").fetchone()[0] == "cancelled"


async def test_bili_decision_can_update_own_impression_without_cancelling_reply(role):
    incoming, _, _, _, _ = linked(role)
    result = answer(incoming)
    result["relationships"] = [{"event_id": incoming["id"], "delta": 1,
                                "impression": "认真讨论草图", "facts": []}]
    scheduler = scheduler_for(role, incoming, result)
    await scheduler.cycle()
    assert len(role.adapter.sent) == 1
    assert role.store.read("relationships", "789")["data"]["affinity"] == 51
