from copy import deepcopy
from datetime import timedelta

import pytest

from bili_assistant import emotions
from bili_assistant.conversations import append
from bili_assistant.errors import AssistantError


def proposal(evidence="event-one", **changes):
    return {"label": "好奇", "reason": "对方介绍了一个具体的新发现",
            "valence_delta": 0.1, "arousal_delta": 0.1, "evidence_ids": [evidence], **changes}


def apply(role, source="event-one", **changes):
    return emotions.apply(role, proposal(source, **changes), [source], role.store.read("growth")["version"])


def test_small_changes_idempotency_and_no_core_persona_change(role):
    persona = role.store.read("persona")
    result = apply(role)
    assert result["applied"] and result["current"]["valence"] == 0.1
    assert not apply(role)["applied"]
    assert len(role.store.records("emotion_events")[0]) == 1
    assert role.store.read("persona") == persona
    assert not role.store.context()["records"]


def test_idle_decay_and_daily_reset_without_writing_on_read(role):
    apply(role)
    original = role.clock()
    original_file = role.store.path("growth").read_bytes()
    role.clock = lambda: original + timedelta(hours=3)
    view = emotions.current(role)
    assert view["valence"] == 0.05 and view["arousal"] == 0.4
    role.clock = lambda: original + timedelta(days=1)
    assert emotions.current(role)["label"] == "平静"
    assert emotions.current(role)["change_today"] == 0
    assert role.store.path("growth").read_bytes() == original_file


def test_step_and_daily_caps(role):
    config = role.store.read("companion")
    config["data"].update(emotion_step_limit=0.05, emotion_daily_change=0.15)
    role.store.write("companion", config["data"], config["version"], "test")
    assert apply(role)["current"]["change_today"] == 0.1
    second = apply(role, "event-two")["current"]
    assert second["valence"] == 0.075 and second["change_today"] == 0.15
    assert not apply(role, "event-three")["applied"]


def test_unseen_or_stale_or_invalid_evidence_cannot_update(role):
    original = role.store.read("growth")
    with pytest.raises(AssistantError, match="引用"):
        emotions.apply(role, proposal("invented"), ["event-one"], original["version"])
    changed = deepcopy(original["data"])
    changed["mood"] = "人工修改"
    role.store.write("growth", changed, original["version"], "user")
    with pytest.raises(AssistantError, match="已经变化"):
        emotions.apply(role, proposal(), ["event-one"], original["version"])
    with pytest.raises(AssistantError, match="协议"):
        apply(role, valence_delta=5)
    assert role.store.read("growth")["data"]["mood"] == "人工修改"


def test_crash_after_growth_write_recovers_once(role, monkeypatch):
    original = role.store.write
    def fail(kind, *args, **kwargs):
        if kind == "emotion_events":
            raise OSError("disk failed")
        return original(kind, *args, **kwargs)
    monkeypatch.setattr(role.store, "write", fail)
    with pytest.raises(OSError):
        apply(role)
    assert emotions.current(role)["valence"] == 0.1
    monkeypatch.setattr(role.store, "write", original)
    emotions.recover(role)
    assert not apply(role)["applied"]
    assert emotions.current(role)["valence"] == 0.1
    assert len(role.store.records("emotion_events")[0]) == 1


def test_manual_edit_blocks_pending_growth_replay(role, monkeypatch):
    original = role.store.write
    def fail(kind, *args, **kwargs):
        if kind == "growth":
            raise OSError("disk failed")
        return original(kind, *args, **kwargs)
    monkeypatch.setattr(role.store, "write", fail)
    with pytest.raises(OSError):
        apply(role)
    monkeypatch.setattr(role.store, "write", original)
    growth = role.store.read("growth")
    growth["data"]["mood"] = "人工调整"
    original("growth", growth["data"], growth["version"], "user")
    emotions.recover(role)
    assert role.store.read("growth")["data"]["mood"] == "人工调整"
    assert "emotion" not in role.store.read("growth")["data"]
    assert not role.store.records("emotion_events")[0]


def test_chat_endpoint_rejects_unrelated_host_message(role, admin):
    stamp = role.clock().isoformat()
    append(role.store, "123", "codex", "other", "user-elsewhere", "别的聊天", "user", "", "received", stamp)
    foreign = role.store.records("conversation_turns")[0][0]["data"]["id"]
    with pytest.raises(AssistantError):
        role.emotion_update(proposal(foreign), role.store.read("growth")["version"])
    own = append(role.store, "123", "codex", "role", "user-here", "当前聊天", "user", "", "received", stamp)[0]
    role.stop(paused=True)
    assert role.emotion_update(proposal(own), role.store.read("growth")["version"])["applied"]
    assert role.store.state("run")["status"] == "paused"
    with pytest.raises(AssistantError):
        admin.emotion_update(proposal(own), role.store.read("growth")["version"])
