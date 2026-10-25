from datetime import datetime, timezone

import pytest

from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError
from bili_assistant.storage import Store


class FakeHost:
    def __init__(self, thread_id="role", states=None):
        self.thread_id = thread_id
        self.states = states if states is not None else {}

    def identity(self):
        return self.thread_id

    def require_active(self, thread_id=None):
        tid = thread_id or self.thread_id
        if self.states.get(tid):
            raise AssistantError("chat_archived", "archived")
        return {"thread_id": tid, "archived": False}


class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.uid = "123"

    async def me(self):
        return {"uid": self.uid}

    async def video_context(self, bvid, page_index=0):
        return {"bvid": bvid, "owner_uid": "888", "title": "测试视频", "coverage": {"kind": "metadata_only"}}

    async def perform(self, kind, payload, policy, before_send):
        before_send()
        self.sent.append((kind, payload))
        if self.fail:
            raise TimeoutError()
        return {"rpid": "456"}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    s.initialize()
    # Recommendation tests use an explicitly configured synthetic persona.
    # Distributed defaults must stay empty for new users.
    persona = s.read("persona")
    persona["data"]["interests"] = ["科技", "游戏开发", "动画"]
    s.write("persona", persona["data"], persona["version"], "test")
    manifest = s.read("manifest")
    manifest["data"]["account_uid"] = "123"
    s.write("manifest", manifest["data"], manifest["version"], "test")
    policy = s.read("policy")
    policy["data"]["min_action_interval_seconds"] = 0
    s.write("policy", policy["data"], policy["version"], "test")
    companion = s.read("companion")
    # Most legacy fixtures isolate content decisions; review tests explicitly enable their lane.
    companion["data"]["knowledge_review"] = False
    companion["data"]["media_fallback"] = "off"
    s.write("companion", companion["data"], companion["version"], "test")
    return s


@pytest.fixture
def role(store):
    engine = Engine(
        store, FakeHost(), FakeAdapter(), lambda: datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    )
    engine.start()
    return engine


@pytest.fixture
def admin(store, role):
    engine = Engine(store, FakeHost("admin", role.host.states), role.adapter, role.clock)
    engine.open("admin")
    return engine


@pytest.fixture
def memory():
    return {
        "schema_version": 1,
        "id": "m1",
        "kind": "user_preference",
        "content": "喜欢实际案例",
        "source": {
            "type": "user_instruction",
            "recorded_at": "2026-10-20T12:00:00+08:00",
            "reference": "conversation:example",
        },
        "confidence": "explicit",
        "tags": ["偏好"],
        "locked": False,
    }
