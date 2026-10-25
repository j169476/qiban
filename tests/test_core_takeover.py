"""Unified core + real Bilibili policy/ledger, with all platform I/O replaced."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from conftest import FakeAdapter, FakeHost
from test_cognition import material

from bili_assistant.connector import BilibiliConnector, normalize
from bili_assistant.conversations import context, event_thread, thread_id
from bili_assistant.engine import Engine
from bili_assistant.storage import Store
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.runtime import Companion

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)


class Platform(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.incoming = []
        self.before = lambda: None
        self.reads = 0

    async def inbox(self, started_at):
        return deepcopy(self.incoming)

    async def video_context(self, bvid, page_index=0):
        self.reads += 1
        return material(bvid)

    async def perform(self, kind, payload, policy, before_send):
        self.before()
        return await super().perform(kind, payload, policy, before_send)


class Model:
    def __init__(self):
        self.calls = []
        self.action = {"capability": "message.reply", "arguments": {"text": "记住了，下次接着聊这个草图。"}}
        self.relationship = None

    async def generate(self, instructions, material, schema, images=(), **options):
        self.calls.append(deepcopy(material))
        if "只评估跨平台身份" in instructions:
            return {"identity_proposals": []}
        result = {"note": "", "action": self.action, "memories": [], "identity_proposals": []}
        if self.relationship:
            result["relationship"] = self.relationship
        return result


def make(store, *, legacy=None, host=None, model=None, platform=None):
    host = host or FakeHost()
    platform = platform or Platform()
    legacy = legacy or Engine(store, host, platform, lambda: NOW)
    model = model or Model()
    connector = BilibiliConnector(legacy)
    core = Companion(store, model, host, [connector], clock=lambda: NOW)
    return core, connector, legacy, platform, model


def incoming(native="dm-789-1", kind="dm_reply", text="我们来讨论昨天的草图吧。"):
    event = {"id": native, "kind": kind, "uid": "789", "received": NOW.isoformat(), "text": text}
    if kind == "reply":
        event.update(oid="321", root="456", parent="456")
    return event


def change_policy(store, **values):
    record = store.read("policy")
    record["data"].update(values)
    store.write("policy", record["data"], record["version"], "test")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dm_reply", "reply"])
async def test_core_alone_receives_decides_replies_and_reuses_native_history(store, kind):
    core, connector, legacy, platform, model = make(store)
    original = incoming(kind=kind)
    platform.incoming = [original]
    assert store.state("run") is None
    core.start()
    assert connector.engine is not legacy
    pending = await core.poll()
    assert len(pending) == 1
    model.relationship = {"delta": 1, "impression": "喜欢讨论草图", "facts": ["共同讨论设计"]}
    first = await core.handle(pending[0])
    assert first["action"]["status"] == "succeeded"
    assert platform.sent[0][0] == kind
    assert store.state("run") is None
    assert store.read("relationships", "789")["data"]["affinity"] == 51
    history = context(store, "123", thread_id("123", *event_thread(original)))
    assert [r["data"]["speaker"] for r in history["turns"]] == ["user", "assistant"]
    assert (await core.handle(pending[0]))["duplicate"]
    assert len(platform.sent) == 1
    platform.incoming = [incoming(native="dm-789-2", kind=kind, text="接着你刚才提到的思路。")]
    second = (await core.poll())[0]
    await core.handle(second)
    assert any(r["data"]["speaker"] == "assistant" for r in model.calls[-1]["conversation"]["turns"])
    assert len(store.records("conversation_turns")[0]) == 4
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM actions WHERE status='succeeded'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM core_actions WHERE status='succeeded'").fetchone()[0] == 2
    core.stop()
    assert store.state("run") is None


@pytest.mark.asyncio
async def test_takeover_preserves_paused_legacy_run(store):
    legacy = Engine(store, FakeHost(), Platform(), lambda: NOW)
    legacy.start()
    legacy.stop(paused=True)
    paused = deepcopy(store.state("run"))
    core, connector, _, _, _ = make(store, legacy=legacy)
    core.start()
    prepared = connector.engine.prepare("like", {"bvid": "BV1RT4y1h7YY"})
    core.stop()
    assert store.state("run") == paused
    with store.db() as db:
        assert db.execute("SELECT status FROM actions WHERE id=?", (prepared["id"],)).fetchone()[0] == "cancelled"


def test_both_directions_refuse_simultaneous_authorities(store):
    legacy = Engine(store, FakeHost(), Platform(), lambda: NOW)
    legacy.start()
    core, _, _, _, _ = make(store, legacy=legacy)
    with pytest.raises(AssistantError) as error:
        core.start()
    assert error.value.code == "legacy_runtime_active"
    assert store.state("core-run") is None
    legacy.stop(paused=True)
    core.start()
    with pytest.raises(AssistantError) as error:
        legacy.start()
    assert error.value.code == "core_runtime_active"
    assert store.state("run")["status"] == "paused"


def test_management_chat_cannot_take_over_role(store):
    host = FakeHost("admin")
    legacy = Engine(store, host, Platform(), lambda: NOW)
    legacy.open("admin")
    core, _, _, _, _ = make(store, legacy=legacy, host=host)
    with pytest.raises(AssistantError) as error:
        core.start()
    assert error.value.code == "mode_denied"
    assert store.state("run") is None
    assert store.state("core-run") is None


@pytest.mark.asyncio
async def test_read_only_run_consumes_message_without_platform_action(store):
    core, _, _, platform, model = make(store)
    model.action = None
    platform.incoming = [incoming()]
    core.start(read_only=True)
    event = (await core.poll())[0]
    assert (await core.handle(event))["action"] is None
    assert await core.poll() == []
    assert model.calls[-1]["capabilities"] == []
    assert not platform.sent
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM core_actions").fetchone()[0] == 0
    assert store.state("run") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["pause", "archive", "policy", "account"])
async def test_inflight_core_control_changes_cancel_actual_bilibili_send(store, stop):
    core, _, _, platform, _ = make(store)
    platform.incoming = [incoming()]
    core.start()
    event = (await core.poll())[0]

    def mutate():
        if stop == "pause":
            core.stop()
        elif stop == "archive":
            core.host.states[core.host.identity()] = True
        elif stop == "policy":
            change_policy(store, daily_videos=store.read("policy")["data"]["daily_videos"] + 1)
        else:
            manifest = store.read("manifest")
            manifest["data"]["account_uid"] = "999"
            store.write("manifest", manifest["data"], manifest["version"], "test")

    platform.before = mutate
    result = await core.dispatch(event, {"capability": "message.reply", "arguments": {"text": "不能发送"}})
    assert result["status"] == "cancelled"
    assert not platform.sent
    assert store.state("run") is None
    with store.db() as db:
        assert db.execute("SELECT status FROM actions").fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_credentials_and_native_daily_quota_remain_enforced(store):
    core, _, _, platform, _ = make(store)
    core.start()
    first = incoming()
    platform.incoming = [first]
    event = (await core.poll())[0]
    platform.uid = "not-the-bound-account"
    mismatch = await core.dispatch(event, {"capability": "message.reply", "arguments": {"text": "失败"}})
    assert mismatch["status"] == "cancelled"
    assert mismatch["error"] == "account_mismatch"
    assert not platform.sent
    platform.uid = "123"
    policy = store.read("policy")
    policy["data"]["actions"]["dm_reply"]["daily_limit"] = 1
    store.write("policy", policy["data"], policy["version"], "test")
    for index in (2, 3):
        platform.incoming = [incoming(native=f"dm-789-{index}")]
        next_event = normalize("123", platform.incoming[0])
        await core.poll()
        result = await core.dispatch(next_event, {"capability": "message.reply", "arguments": {"text": "收到"}})
        assert result["status"] == ("succeeded" if index == 2 else "cancelled")
        if index == 3:
            assert result["error"] == "quota_exhausted"
    assert len(platform.sent) == 1
    assert store.state("run") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ["succeeded", "uncertain"])
async def test_takeover_deduplicates_native_receipts_from_previous_legacy_run(store, prior_status):
    host, platform = FakeHost(), Platform()
    legacy = Engine(store, host, platform, lambda: NOW)
    legacy.start()
    platform.incoming = [incoming()]
    await legacy.inbox()
    prepared = legacy.prepare("dm_reply", {"event_id": incoming()["id"], "text": "旧调度的回复"})
    platform.fail = prior_status == "uncertain"
    assert (await legacy.execute(prepared["id"]))["status"] == prior_status
    legacy.stop(paused=True)
    paused = deepcopy(store.state("run"))
    core, _, _, _, _ = make(store, legacy=legacy, host=host, platform=platform)
    core.start()
    event = (await core.poll())[0]
    result = await core.dispatch(event, {"capability": "message.reply", "arguments": {"text": "新调度不能重发"}})
    assert result["status"] == prior_status
    assert result["receipt"]["legacy_action_id"] == prepared["id"]
    assert len(platform.sent) == 1
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
    assert store.state("run") == paused


@pytest.mark.asyncio
async def test_uncertain_core_send_is_not_replayed_after_explicit_restart(store):
    core, _, legacy, platform, model = make(store)
    platform.incoming = [incoming()]
    platform.fail = True
    core.start()
    event = (await core.poll())[0]
    assert (await core.handle(event))["action"]["status"] == "uncertain"
    core.stop()
    restarted, _, _, _, _ = make(store, legacy=legacy, platform=platform, model=model)
    restarted.start()
    assert (await restarted.handle(event))["duplicate"]
    assert len(platform.sent) == 1
    assert store.state("run") is None


@pytest.mark.asyncio
async def test_takeover_keeps_native_quota_spent_by_legacy_controller(store):
    host, platform = FakeHost(), Platform()
    legacy = Engine(store, host, platform, lambda: NOW)
    legacy.start()
    platform.incoming = [incoming()]
    await legacy.inbox()
    prepared = legacy.prepare("dm_reply", {"event_id": incoming()["id"], "text": "旧调度已使用当日额度"})
    await legacy.execute(prepared["id"])
    legacy.stop(paused=True)
    paused = deepcopy(store.state("run"))
    policy = store.read("policy")
    policy["data"]["actions"]["dm_reply"]["daily_limit"] = 1
    store.write("policy", policy["data"], policy["version"], "test")
    core, _, _, _, _ = make(store, legacy=legacy, host=host, platform=platform)
    core.start()
    original = incoming(native="dm-789-2")
    platform.incoming = [original]
    await core.poll()
    result = await core.dispatch(normalize("123", original), {
        "capability": "message.reply", "arguments": {"text": "这次不能超额"}})
    assert result["status"] == "cancelled"
    assert result["error"] == "quota_exhausted"
    assert len(platform.sent) == 1
    assert store.state("run") == paused


@pytest.mark.asyncio
async def test_bilibili_uses_one_native_quota_authority_and_retains_policy_switch(store):
    core, _, _, platform, _ = make(store)
    core.start()
    # A core-only platform budget must not add a hidden 20-action cap on top of Bili policy.
    key = stable_key("action-budget", "bilibili", "123", "2026-10-22")
    store.set_state(key, 20)
    platform.incoming = [incoming()]
    event = (await core.poll())[0]
    result = await core.dispatch(event, {"capability": "message.reply", "arguments": {"text": "按平台额度执行"}})
    assert result["status"] == "succeeded"
    assert store.state(key) == 20
    policy = store.read("policy")
    policy["data"]["actions"]["dm_reply"]["enabled"] = False
    store.write("policy", policy["data"], policy["version"], "test")
    second = incoming(native="dm-789-2")
    platform.incoming = [second]
    await core.poll()
    result = await core.dispatch(normalize("123", second), {
        "capability": "message.reply", "arguments": {"text": "功能关闭后不能发送"}})
    assert result["status"] == "cancelled"
    assert result["error"] == "action_disabled"
    assert len(platform.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["like", "favorite", "coin", "comment"])
async def test_core_video_actions_keep_native_material_and_units_checks(store, capability):
    core, _, _, platform, _ = make(store)
    policy = store.read("policy")
    policy["data"]["actions"][capability].update(enabled=True, daily_limit=10)
    store.write("policy", policy["data"], policy["version"], "test")
    core.start()
    platform.incoming = [incoming()]
    event = (await core.poll())[0]
    args = {"bvid": "BV1RT4y1h7YY"}
    if capability == "coin":
        args["units"] = 2
    if capability == "comment":
        args["text"] = "这个最小案例的解释很清楚。"
    result = await core.dispatch(event, {"capability": "bilibili." + capability, "arguments": args})
    assert result["status"] == "succeeded"
    assert platform.reads == 1
    assert platform.sent[0][0] == capability
    with store.db() as db:
        action = db.execute("SELECT kind,units FROM actions").fetchone()
        assert action["kind"] == capability
        assert action["units"] == (2 if capability == "coin" else 1)
    assert store.state("run") is None


@pytest.mark.asyncio
async def test_core_video_reads_share_native_video_quota_and_stop_guard(store):
    core, connector, _, platform, _ = make(store)
    change_policy(store, daily_videos=1)
    core.start(read_only=True)
    reference = {"platform": "bilibili", "kind": "video", "id": "BV1RT4y1h7YY"}
    result = await connector.read(reference)
    assert result["segments"][0]["text"] == material(reference["id"])["segments"][0]["text"]
    assert "bvid" not in result["coverage"]
    assert store.records("source_snapshots")[0]
    with pytest.raises(AssistantError) as error:
        await connector.read({**reference, "id": "BV1xx411c7mD"})
    assert error.value.code == "video_quota"
    core.stop()
    with pytest.raises(AssistantError):
        await connector.read(reference)
    assert platform.reads == 1
    assert store.state("run") is None


@pytest.mark.asyncio
async def test_native_success_before_archive_crash_recovers_on_direct_core_replay(store, monkeypatch):
    core, connector, _, platform, model = make(store)
    core.start()
    platform.incoming = [incoming()]
    event = (await core.poll())[0]
    original_record = connector.engine.record_action

    def interrupted(record):
        raise RuntimeError("simulated crash after native receipt commit")

    monkeypatch.setattr(connector.engine, "record_action", interrupted)
    result = await core.handle(event)
    assert result["action"]["status"] == "uncertain"
    assert len(platform.sent) == 1
    with store.db() as db:
        assert db.execute("SELECT status FROM actions").fetchone()[0] == "succeeded"
    assert all(r["data"]["speaker"] != "assistant" for r in store.records("conversation_turns")[0])
    monkeypatch.setattr(connector.engine, "record_action", original_record)
    # No poll/inbox call occurs here: duplicate handling itself must repair missing history.
    assert (await core.handle(event))["duplicate"]
    assert (await core.handle(event))["duplicate"]
    replies = [r for r in store.records("conversation_turns")[0] if r["data"]["speaker"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["data"]["content"] == model.action["arguments"]["text"]
    assert replies[0]["data"]["status"] == "sent"
    assert len(platform.sent) == 1
    assert len(model.calls) == 1
    with store.db() as db:
        assert db.execute("SELECT status FROM core_actions").fetchone()[0] == "uncertain"


@pytest.mark.asyncio
async def test_binding_two_store_instances_uses_core_lock_for_nested_native_guards(store):
    other = Store(store.root)
    store.lock.timeout = other.lock.timeout = 0.05
    assert store.lock is not other.lock
    host, platform = FakeHost(), Platform()
    legacy = Engine(other, host, platform, lambda: NOW)
    core, connector, _, _, _ = make(store, legacy=legacy, host=host, platform=platform)
    assert connector.engine.store is core.store
    assert legacy.store is other
    core.start()
    platform.incoming = [incoming()]
    event = (await core.poll())[0]
    assert (await core.handle(event))["action"]["status"] == "succeeded"
    assert len(platform.sent) == 1
    assert other.state("run") is None
