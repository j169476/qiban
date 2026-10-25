"""New inbound source events remain frozen before they become identity evidence."""
from copy import deepcopy
from dataclasses import replace

import pytest
from test_identity_decisions import answer

from bili_assistant.cognition import save_learning
from bili_assistant.connector import identity_packet, normalize
from bili_assistant.scheduler import Scheduler
from companion_core import events
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.identity import IdentityService


def incoming(role):
    return {"id": "dm-789-new-identity", "kind": "dm_reply", "uid": "789", "display_name": "测试用户",
            "received": role.clock().isoformat(), "text": "我的QQ号是7654321，昨天讨论过蓝色桌面的草图。"}


def scheduler_for(role, item, change=lambda: None, *, claim=True):
    class Inbox(type(role.adapter)):
        async def inbox(self, started_at):
            return [item]

        async def feed(self, **kwargs):
            return []

    role.adapter = Inbox()
    scheduler = Scheduler(role, role.run()["generation"])

    async def model(instructions, context, schema, images=(), **kwargs):
        assert not context["identity_people"][0]["person"]["inferred"]
        assert IdentityService(role.store).links() == []
        result = answer(item)
        result["memory"] = "用户在新私信中声明了自己的QQ号。"
        if claim:
            proof = context["identity_evidence"][0]
            result["identity_proposals"] = [{
                "left": proof["event"]["actor"], "right": ActorRef("qq", "7654321").as_dict(),
                "kind": "claim", "reason": "本人明确声明自己的QQ号", "specific_correspondence": False,
                "evidence": [{"event_id": proof["event"]["id"], "version": proof["version"],
                              "quote": proof["event"]["text"]}],
            }]
        change()
        return deepcopy(result)

    async def noop():
        pass

    scheduler.model = model
    scheduler.maintenance = scheduler.maybe_reflect = noop
    return scheduler


@pytest.mark.parametrize("change_kind", ["edit", "delete"])
async def test_new_input_change_during_model_prevents_identity_memory_and_send(role, change_kind):
    item = incoming(role)
    source = normalize("123", item)
    corrected = replace(source, text="之前写错了，那不是我的QQ号，不要建立关联。")

    def change():
        if change_kind == "edit":
            events.ingest(role.store, corrected)
        else:
            events.forget(role.store, source.id)

    scheduler = scheduler_for(role, item, change)
    with pytest.raises(AssistantError) as error:
        await scheduler.cycle()
    assert error.value.code == "context_changed"
    assert not role.adapter.sent
    assert not role.store.records("memories")[0]
    assert IdentityService(role.store).links() == []
    if change_kind == "edit":
        assert events.get(role.store, source.id)["event"]["text"] == corrected.text
    else:
        with pytest.raises(AssistantError):
            events.get(role.store, source.id)


async def test_new_input_edit_after_model_before_save_is_never_overwritten(role):
    item = incoming(role)
    source = normalize("123", item)
    scheduler = scheduler_for(role, item)
    material = {"events": [item], "video": None}
    decision = await scheduler.decide(material)
    corrected = replace(source, text="修正：之前提到的是别人的QQ号。")
    events.ingest(role.store, corrected)
    with pytest.raises(AssistantError) as error:
        save_learning(role, material, decision)
    assert error.value.code == "context_changed"
    assert events.get(role.store, source.id)["event"]["text"] == corrected.text
    assert not role.store.records("memories")[0]
    assert IdentityService(role.store).links() == []


async def test_new_unlinked_input_change_at_actual_send_is_cancelled(role):
    item = incoming(role)
    source = normalize("123", item)
    scheduler = scheduler_for(role, item, claim=False)
    original = role.adapter.perform

    async def edit_before_send(kind, payload, policy, before_send):
        events.ingest(role.store, replace(source, text="刚才那条消息的内容有误，请勿回复。"))
        return await original(kind, payload, policy, before_send)

    role.adapter.perform = edit_before_send
    await scheduler.cycle()
    assert not role.adapter.sent
    assert IdentityService(role.store).links() == []
    with role.store.db() as db:
        assert db.execute("SELECT status FROM actions").fetchone()[0] == "cancelled"


def test_material_packet_does_not_replace_an_existing_newer_event(role):
    item = incoming(role)
    source = normalize("123", item)
    corrected = replace(source, text="这条更正已经保存，旧材料不得覆盖。")
    events.ingest(role.store, corrected)
    with pytest.raises(AssistantError) as error:
        identity_packet(role, [item], with_dependencies=True)
    assert error.value.code == "context_changed"
    assert events.get(role.store, source.id)["event"]["text"] == corrected.text


def test_direct_save_learning_initializes_only_a_previously_missing_source(role):
    item = incoming(role)
    source = normalize("123", item)
    with pytest.raises(AssistantError):
        events.get(role.store, source.id)
    result = save_learning(role, {"events": [item], "video": None}, answer(item))
    assert result["memory"]
    assert events.get(role.store, source.id)["event"]["text"] == item["text"]


@pytest.mark.parametrize("change_kind", ["edit", "delete"])
def test_direct_save_without_packet_still_refuses_replaced_or_deleted_sources(role, change_kind):
    item = incoming(role)
    source = normalize("123", item)
    events.ingest(role.store, source)
    if change_kind == "edit":
        events.ingest(role.store, replace(source, text="用户已经修正此声明，旧材料不能覆盖。"))
    else:
        events.forget(role.store, source.id)
    with pytest.raises(AssistantError) as error:
        save_learning(role, {"events": [item], "video": None}, answer(item))
    assert error.value.code == "context_changed"
    assert not role.store.records("memories")[0]
