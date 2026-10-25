from datetime import timedelta

import pytest

from bili_assistant.cognition import packet, verify_versions
from bili_assistant.conversations import context_for_events, record_event, thread_id
from bili_assistant.errors import AssistantError


def seed(role, uid="789", phrase="北岸灯塔的暗号是钴蓝纸鹤。"):
    for index in range(32):
        item = {"id": f"dm-{uid}-{index}", "kind": "dm_reply", "uid": uid,
                "text": phrase if index == 4 else f"天气记录 {index}",
                "received": (role.clock() + timedelta(seconds=index)).isoformat()}
        record_event(role.store, "123", item)
    return {**item, "id": f"dm-{uid}-current", "text": "以前的碰面细节"}


def test_legacy_decision_packet_reuses_expansion_and_pins_recalled_versions(role):
    item = seed(role)
    baseline, _ = packet(role, {"events": [item]})
    assert not baseline["conversation_threads"][0]["recalled"]
    result, versions = packet(role, {"events": [item]}, queries=["北岸灯塔"])
    recalled = result["conversation_threads"][0]["recalled"]
    target = next(row for row in recalled if "钴蓝纸鹤" in row["data"]["content"])
    assert ("conversation_turns", target["data"]["id"], target["version"]) in versions
    verify_versions(role.store, versions)
    source = role.store.read("conversation_turns", target["data"]["id"])
    source["data"]["content"] = "北岸灯塔改用琥珀风铃。"
    role.store.write("conversation_turns", source["data"], source["version"], "fixture", source["data"]["id"])
    with pytest.raises(AssistantError):
        verify_versions(role.store, versions)


def test_each_event_thread_uses_its_own_question_and_shared_budget(role):
    first = seed(role, "789")
    second = seed(role, "780", "海角咖啡馆的暗号是琥珀风铃。")
    result = context_for_events(role.store, "123", [
        {**first, "text": "北岸灯塔"}, {**second, "text": "海角咖啡馆"}])
    assert sum(row["context_chars"] for row in result) <= 12000
    assert result[0]["thread_id"] == thread_id("123", "dm", "789")
    assert "钴蓝纸鹤" in str(result[0]["recalled"]) and "琥珀风铃" not in str(result[0])
    assert "琥珀风铃" in str(result[1]["recalled"]) and "钴蓝纸鹤" not in str(result[1])
