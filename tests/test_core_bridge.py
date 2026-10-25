from copy import deepcopy

import pytest

from bili_assistant.api import call
from bili_assistant.cognition import packet, save_learning
from bili_assistant.connector import BilibiliConnector, migrate_legacy, normalize
from bili_assistant.errors import AssistantError
from companion_core.contracts import ActorRef
from companion_core.identity import IdentityService
from companion_core.people import profile


def incoming(role):
    return {"id": "dm-789-1", "kind": "dm_reply", "uid": "789", "received": role.clock().isoformat(),
            "text": "我的QQ号是 7654321，以后也会用那个号和你讨论昨天的草图。"}


def test_bilibili_decision_uses_core_identity_and_existing_relationship_store(role):
    event = incoming(role)
    material = {"events": [event], "video": None}
    context, _ = packet(role, material)
    evidence = context["identity_evidence"][0]
    decision = {"emotion": None, "memory": "", "knowledge": "", "skill": None, "relationships": [],
                "interest_signals": [], "tags": [], "identity_proposals": [{
                    "left": evidence["event"]["actor"], "right": ActorRef("qq", "7654321").as_dict(),
                    "kind": "claim", "reason": "用户第一人称明确提供自己的另一个平台账号",
                    "specific_correspondence": False, "evidence": [{"event_id": evidence["event"]["id"],
                        "version": evidence["version"], "quote": event["text"]}],
                }]}
    save_learning(role, material, decision)
    assert IdentityService(role.store).links()[0]["effective_status"] == "suspected"
    role.relationship("789", "测试用户", 1, event["id"], "讨论过草图")
    assert profile(role.store, ActorRef("bilibili", "789"))["impressions"][0]["affinity"] == 51


@pytest.mark.asyncio
async def test_management_identity_controls_do_not_start_role(admin, role):
    role.stop(paused=True)
    old = deepcopy(role.store.state("run"))
    assert (await call(admin, "core_migrate", {}))["role_started"] is False
    assert (await call(admin, "identity_status", {}))["links"] == []
    assert role.store.state("run") == old
    with pytest.raises(AssistantError):
        await call(role, "identity_revoke", {"link_id": "x", "expected_version": "x", "reason": "test"})


@pytest.mark.asyncio
async def test_bilibili_connector_uses_same_existing_action_ledger(role):
    event = incoming(role)
    async def inbox(started_at):
        return [event]
    role.adapter.inbox = inbox
    connector = BilibiliConnector(role)
    items, _ = await connector.receive({})
    assert items[0].id == normalize("123", event).id
    args = {"text": "收到", "_event": items[0].as_dict()}
    one = await connector.execute("message.reply", args, guard=role.role_run)
    two = await connector.execute("message.reply", args, guard=role.role_run)
    assert one.status == two.status == "succeeded"
    assert len(role.adapter.sent) == 1


def test_legacy_mapping_is_additive_and_repeatable(role):
    role.relationship("789", "old", 1, "old-event")
    role.stop(paused=True)
    before = {p: p.read_bytes() for p in role.store.root.rglob("*.yaml") if "history" not in p.parts}
    run = deepcopy(role.store.state("run"))
    assert migrate_legacy(role.store)["legacy_files_rewritten"] == 0
    assert migrate_legacy(role.store)["normalized_events"] == 0
    assert all(p.read_bytes() == value for p, value in before.items())
    assert role.store.state("run") == run
