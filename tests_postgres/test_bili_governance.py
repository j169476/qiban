import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from bili_assistant.connector import BilibiliConnector
from bili_assistant.engine import Engine
from bili_assistant.profiles import profile
from companion_core.errors import AssistantError
from companion_core.runtime import Companion
from tests_postgres.test_runtime import Host

NOW = datetime(2026, 10, 23, 6, tzinfo=timezone.utc)


@pytest.fixture
def pg_profile():
    return profile()


class Platform:
    def __init__(self):
        self.sent = []
        self.incoming = [{"id": "dm-" + uid, "uid": uid, "kind": "dm_reply", "received": NOW.isoformat(),
                          "text": "private-canary-" + uid} for uid in ("11", "22")]
        self.incoming.append({"id": "reply-33", "uid": "33", "kind": "reply", "received": NOW.isoformat(),
                              "text": "public-topic", "oid": "100", "root": "200", "parent": "200"})

    async def inbox(self, since):
        return deepcopy(self.incoming)

    async def me(self):
        return {"uid": "123"}

    async def feed(self, **kwargs):
        return []

    async def perform(self, kind, payload, policy, before_send):
        before_send()
        self.sent.append((kind, payload))
        return {"rpid": "321"}


class Model:
    def __init__(self):
        self.calls = []

    async def generate(self, instructions, context, schema, images=(), **options):
        self.calls.append(deepcopy(context))
        if "event" in context:
            return {"note": "", "action": {"capability": "message.reply", "arguments": {"text": "私密回复"}},
                    "memories": [context["event"]["text"]], "identity_proposals": [], "relationship": None}
        event = context["material"]["events"][0]
        return {"note": "", "memory": "public-memory", "knowledge": "", "tags": [], "video_topics": [], "emotion": None,
                "skill": None, "relationships": [], "interest_signals": [], "identity_proposals": [],
                "native_request": None, "video_gap": None,
                "action": {"kind": "reply", "payload": {"event_id": event["id"], "text": "公开回复", "bvid": "", "uid": "", "units": 1}}}


def patch(store, kind, **changes):
    record = store.read(kind)
    store.write(kind, {**record["data"], **changes}, record["version"], "test")


@pytest.mark.asyncio
async def test_private_inbox_public_learning_and_sends_are_separate(pg_store):
    patch(pg_store, "manifest", account_uid="123")
    patch(pg_store, "policy", min_action_interval_seconds=0)
    patch(pg_store, "companion", memory_maintenance=False, daily_reflection=False, knowledge_review=False,
          media_fallback="off", search_expansion=False, native_tools_enabled=False)
    patch(pg_store, "persona", core={"description": "private-persona-canary", "speaking_style": ["private-style"]})
    patch(pg_store, "growth", reason="private-growth-canary")
    patch(pg_store, "preferences", blocked_topics=["private-preference-canary"])
    pg_store.set_state("reflection-pending:123", {"leak": "private-state-canary"})
    platform, model, host = Platform(), Model(), Host()
    core = Companion(pg_store, model, host, [BilibiliConnector(Engine(pg_store, host, platform, lambda: NOW))], clock=lambda: NOW)
    core.start()
    await core.cycle()
    assert len(model.calls) == 3 and len(platform.sent) == 3
    for context in model.calls:
        serialized = json.dumps(context, ensure_ascii=False)
        if "event" in context:
            own = context["event"]["actor"]["user_id"]
            assert "private-canary-" + own in serialized
            assert "private-canary-" + ("22" if own == "11" else "11") not in serialized
        else:
            assert "public-topic" in serialized
            assert "private-canary" not in serialized
        assert "private-persona-canary" not in serialized
        assert "private-growth-canary" not in serialized
        assert "private-preference-canary" not in serialized
        assert "private-state-canary" not in serialized
    core.stop()
    assert pg_store.state("core-run")["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_private_images_reach_only_own_decision_and_cleanup_on_failure(pg_store, monkeypatch, failed):
    from pathlib import Path

    from bili_assistant.connector import normalize
    from bili_assistant.rich_content import image_attachment
    from companion_core import events

    patch(pg_store, "manifest", account_uid="123")
    patch(pg_store, "policy", min_action_interval_seconds=0)
    platform, host = Platform(), Host()
    paths = []

    async def download(store, url):
        assert url == "https://i0.hdslb.com/synthetic-private.png"
        path = store.root / "private.png"
        path.write_bytes(b"synthetic-only")
        paths.append(path)
        return str(path)

    class Visual(Model):
        async def generate(self, instructions, context, schema, images=(), **options):
            assert len(images) == 1 and Path(images[0]).exists()
            assert "private-canary-22" not in str(context)
            assert context["event_material"]["images_read"] == 1
            if failed:
                raise AssistantError("model_failed", "synthetic")
            return await super().generate(instructions, context, schema, images, **options)

    monkeypatch.setattr("bili_assistant.media.download_image", download)
    core = Companion(pg_store, Visual(), host, [BilibiliConnector(Engine(pg_store, host, platform, lambda: NOW))], clock=lambda: NOW)
    core.start()
    incoming = platform.incoming[0]
    incoming["attachments"] = [image_attachment("https://i0.hdslb.com/synthetic-private.png")]
    # Reuse the normal Bili collector so native reply targets remain grounded.
    await core.poll()
    event = normalize("123", incoming)
    assert events.get(pg_store, event.id)
    if failed:
        with pytest.raises(AssistantError):
            await core.handle(event)
        assert not platform.sent
    else:
        assert (await core.handle(event))["action"]["status"] == "succeeded"
    assert paths and all(not path.exists() and not path.parent.exists() for path in paths)
    core.stop()
