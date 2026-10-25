"""Concurrency, read-only consumption and complete conversation regressions."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

import pytest
import yaml
from test_platform_core import NOW, Adapter, Host, Model, event, proposal, reference, suspected

from companion_core import conversations, events
from companion_core.contracts import ActionResult, Capability
from companion_core.errors import AssistantError
from companion_core.runtime import Companion
from companion_core.schema import obj
from companion_core.storage import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "core")
    assert store.initialize()["ok"]
    model, adapter, host = Model(), Adapter(), Host()
    core = Companion(store, model, host, [adapter], clock=lambda: datetime.fromisoformat(NOW))
    core.start()
    return store, core, model, adapter, host


def history(store, core, item):
    scope = core.conversation_scope(item)
    return conversations.records(store, scope["account"],
                                 conversations.thread_id(scope["account"], scope["channel"], scope["key"]))


@pytest.mark.asyncio
async def test_sent_reply_is_available_next_turn_once_and_in_its_own_thread(setup):
    store, core, model, adapter, _ = setup
    first = event(text="请记住我们对话的上一句话")
    assert (await core.handle(first))["action"]["status"] == "succeeded"
    assert (await core.handle(first))["duplicate"]
    await core.handle(event(native="two", text="你刚才说什么？"))
    context = model.calls[-1]["conversation"]
    assert [r["data"]["content"] for r in context["turns"] if r["data"]["speaker"] == "assistant"] == ["收到"]
    assert len(history(store, core, first)) == 4
    assert len(adapter.sent) == 2
    await core.handle(event(native="other", conversation="other-room"))
    assert all(r["data"]["speaker"] == "user" for r in model.calls[-1]["conversation"]["turns"])


@pytest.mark.parametrize("status", ["failed", "uncertain", "cancelled"])
@pytest.mark.asyncio
async def test_unsent_or_uncertain_actions_are_never_archived_as_spoken(setup, status):
    store, core, _, adapter, _ = setup

    async def execute(*args, guard, **kwargs):
        guard()
        return ActionResult(status, error="simulated")

    adapter.execute = execute
    item = event()
    assert (await core.handle(item))["action"]["status"] == status
    assert [r["data"]["speaker"] for r in history(store, core, item)] == ["user"]


@pytest.mark.asyncio
async def test_reply_archive_recovers_after_ledger_commit_without_resending(setup, monkeypatch):
    store, core, _, adapter, _ = setup
    original = core._record_reply

    def interrupted(*args):
        raise RuntimeError("process interrupted before conversation archive")

    monkeypatch.setattr(core, "_record_reply", interrupted)
    item = event()
    action = {"capability": "message.reply", "arguments": {"text": "已确认发送的回答"}}
    with pytest.raises(RuntimeError, match="interrupted"):
        await core.dispatch(item, action)
    monkeypatch.setattr(core, "_record_reply", original)
    assert (await core.dispatch(item, action))["duplicate"]
    assert (await core.dispatch(item, action))["duplicate"]
    assert len(adapter.sent) == 1
    assert [r["data"]["content"] for r in history(store, core, item)] == ["已确认发送的回答"]
    # A later deliberate local deletion remains authoritative even on replay.
    store.path("conversation_turns", history(store, core, item)[0]["data"]["id"]).unlink()
    await core.dispatch(item, action)
    assert history(store, core, item) == []


@pytest.mark.asyncio
async def test_read_only_omits_writes_and_consumes_valid_learning_even_for_stale_model_action(setup):
    store, core, model, adapter, _ = setup
    core.stop()
    core.start(read_only=True)
    item = event(text="我们约好了下次继续讨论草图")
    adapter.incoming = [item]
    model.memories = ["本会话约定下次继续讨论草图"]
    schemas = []
    generate = model.generate

    async def capture(instructions, context, schema, *args, **kwargs):
        schemas.append(schema)
        return await generate(instructions, context, schema, *args, **kwargs)

    model.generate = capture
    assert await core.poll() == [item]
    result = await core.handle(item)
    assert result["action"] is None and result["decision"]["action"] is None
    assert model.calls[0]["runtime"]["read_only"] is True
    assert model.calls[0]["capabilities"] == []
    assert schemas[0]["properties"]["action"]["anyOf"] == [{"type": "null"}]
    assert (await core.handle(item))["duplicate"]
    assert await core.poll() == []
    assert len(model.calls) == 1 and not adapter.sent
    assert len(store.records("memories")[0]) == 1
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM core_actions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_read_only_retains_explicit_read_capabilities(setup):
    store, core, model, adapter, _ = setup
    adapter.capabilities = lambda: [Capability("content.read", "read", obj({"reference": {"type": "string"}}), "read")]

    async def generate(instructions, context, schema, *args, **kwargs):
        assert [c["name"] for c in context["capabilities"]] == ["content.read"]
        return {"note": "", "action": {"capability": "content.read", "arguments": {"reference": "one"}},
                "memories": [], "identity_proposals": []}

    model.generate = generate
    core.stop()
    core.start(read_only=True)
    assert (await core.handle(event()))["action"]["status"] == "succeeded"
    assert [r["data"]["speaker"] for r in history(store, core, event())] == ["user"]


@pytest.mark.parametrize("old_host_fails", [True, False])
def test_old_host_check_never_stops_or_returns_replacement_run(setup, old_host_fails):
    store, core, _, _, host = setup
    previous = store.state("core-run")
    replacement = {**previous, "generation": "replacement-generation"}

    def switch_run(*args):
        store.set_state("core-run", replacement)
        if old_host_fails:
            raise AssistantError("chat_archived", "old owner was archived")

    host.require_active = switch_run
    with pytest.raises(AssistantError) as error:
        core.guard(previous["generation"])
    assert error.value.code == ("chat_archived" if old_host_fails else "stale_generation")
    assert store.state("core-run") == replacement
    assert not (store.runtime / ("core-stop-" + replacement["generation"])).exists()


def test_stop_rechecks_generation_after_writing_old_stop_marker(setup, monkeypatch):
    from companion_core import runtime

    store, core, _, _, _ = setup
    old = store.state("core-run")
    replacement = {**old, "generation": "new-run"}
    write = runtime.atomic_write

    def concurrent_start(path, payload):
        write(path, payload)
        store.set_state("core-run", replacement)

    monkeypatch.setattr(runtime, "atomic_write", concurrent_start)
    assert core.stop(generation=old["generation"])["stopped"] is False
    assert store.state("core-run") == replacement
    assert not (store.runtime / "core-stop-new-run").exists()


def summary(store, core, item):
    scope = core.conversation_scope(item)
    tid = conversations.thread_id(scope["account"], scope["channel"], scope["key"])
    covered = history(store, core, item)
    return store.write("conversation_summaries", {
        "schema_version": 1, "id": "summary-test", "account_uid": scope["account"], "thread_id": tid,
        "content": "用户要求记住约定，助手已经回答收到。", "previous": None,
        "turn_refs": [{"id": r["data"]["id"], "version": r["version"]} for r in covered],
        "source": {"type": "reflection", "recorded_at": NOW, "reference": tid}, "locked": False,
    }, None, "test", "summary-test")


@pytest.mark.asyncio
async def test_existing_compression_summary_is_used_with_live_tail(setup):
    store, core, model, _, _ = setup
    item = event()
    await core.handle(item)
    saved = summary(store, core, item)
    await core.handle(replace(event(native="later", text="继续我们之前的约定"), occurred_at="2026-10-22T04:01:00+00:00"))
    context = model.calls[-1]["conversation"]
    assert context["summary"]["content"] == saved["data"]["content"]
    assert context["summary"]["covered_turns"] == 2
    assert [r["data"]["content"] for r in context["turns"]] == ["继续我们之前的约定"]


@pytest.mark.asyncio
async def test_changed_summary_invalidates_decision_before_memory_or_send(setup):
    store, core, model, adapter, _ = setup
    item = event()
    await core.handle(item)
    saved = summary(store, core, item)
    model.memories = ["不应保存的过期总结"]

    def change():
        saved["data"]["content"] = "用户已经改正这个约定"
        store.write("conversation_summaries", saved["data"], saved["version"], "user", "summary-test")

    model.before = change
    with pytest.raises(AssistantError) as error:
        await core.handle(replace(event(native="later"), occurred_at="2026-10-22T04:01:00+00:00"))
    assert error.value.code == "context_changed"
    assert len(adapter.sent) == 1 and store.records("memories")[0] == []


@pytest.mark.parametrize("when", ["model", "before_send"])
@pytest.mark.asyncio
async def test_revoked_identity_invalidates_model_commit_and_send(setup, when):
    store, core, model, adapter, _ = setup
    service, first, second, _, evidence = suspected(store)
    hard = service.apply(proposal(first.actor, second.actor, [evidence, reference(store, second)], "correspondence", True))
    generate = model.generate

    def revoke():
        service.revoke(hard["data"]["id"], hard["version"], "管理端确认不是同一人")

    async def reply(instructions, context, schema, *args, **kwargs):
        result = await generate(instructions, context, schema, *args, **kwargs)
        if "event" in context and when == "model":
            revoke()
            result["memories"] = ["两个平台是同一个人的错误记忆"]
        return result

    model.generate = reply
    if when == "before_send":
        adapter.before = revoke
        assert (await core.handle(first))["action"]["status"] == "cancelled"
    else:
        with pytest.raises(AssistantError) as error:
            await core.handle(first)
        assert error.value.code == "context_changed"
    assert not adapter.sent and store.records("memories")[0] == []
    assert not service.person(first.actor)["inferred"]


@pytest.mark.asyncio
async def test_own_relationship_update_does_not_cancel_valid_reply(setup):
    store, core, model, adapter, _ = setup
    item = event()
    core.relationship(item.actor.key, "friend", 1, "prior", "喜欢草图", ["昨天讨论过草图"])
    generate = model.generate

    async def update(instructions, context, schema, *args, **kwargs):
        result = await generate(instructions, context, schema, *args, **kwargs)
        if "event" in context:
            result["relationship"] = {"delta": 1, "impression": "继续讨论草图", "facts": ["愿意再聊"]}
        return result

    model.generate = update
    assert (await core.handle(item))["action"]["status"] == "succeeded"
    assert len(adapter.sent) == 1
    assert store.read("relationships", item.actor.key)["data"]["interactions"] == 2
    assert store.state("core-processed:" + item.id) == events.get(store, item.id)["version"]


def memory_document(item_id, source):
    return {"schema_version": 1, "id": item_id, "kind": "experience", "content": "我们一起讨论蓝色桌面草图",
            "source": {"type": "interaction", "recorded_at": NOW, "reference": source.id},
            "confidence": "observed", "tags": [], "locked": False}


def test_large_memory_library_checks_source_visibility_in_one_database_query(setup, monkeypatch):
    store, core, _, _, _ = setup
    source = event(native="source")
    events.ingest(store, source)
    for number in range(1000):
        item_id = f"large-{number:04}"
        store.path("memories", item_id).write_text(
            yaml.safe_dump(memory_document(item_id, source), allow_unicode=True), encoding="utf-8"
        )
    core.memory_context(event())  # Warm the normal YAML/index cache before measuring reads.
    database = store.db
    statements, connections = [], []

    @contextmanager
    def counted_database():
        connections.append(True)
        with database() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "db", counted_database)
    dependencies = {}
    context = core.memory_context(event(), dependencies=dependencies)
    assert context["total_records"] == 1000 and context["records"]
    assert sum("FROM core_events" in statement for statement in statements) == 1
    assert len(connections) < 20
    assert dependencies == {source.id: events.get(store, source.id)["version"]}


@pytest.mark.parametrize("change", ["thread", "text", "delete"])
@pytest.mark.asyncio
async def test_memory_source_changes_invalidate_pending_decision(setup, change):
    store, core, model, adapter, _ = setup
    source = event(native="source")
    events.ingest(store, source)
    store.write("memories", memory_document("remembered", source), None, "test", "remembered")

    def revise_source():
        if change == "thread":
            events.ingest(store, replace(source, conversation="other-room"))
        elif change == "text":
            events.ingest(store, replace(source, text="已经更正的原话"))
        else:
            events.forget(store, source.id)

    model.before = revise_source
    model.memories = ["不应保留的旧资料推导"]
    with pytest.raises(AssistantError) as error:
        await core.handle(event(native="question"))
    assert error.value.code == "context_changed"
    assert not adapter.sent
    assert [r["data"]["id"] for r in store.records("memories")[0]] == ["remembered"]


@pytest.mark.parametrize("change", ["text", "thread", "delete"])
@pytest.mark.asyncio
async def test_old_polled_event_cannot_overwrite_new_source_or_resurrect_deleted_event(setup, change):
    store, core, model, adapter, _ = setup
    original = event()
    adapter.incoming = [original]
    queued = (await core.poll())[0]
    if change == "delete":
        events.forget(store, original.id)
        assert await core.handle(queued) == {"ignored": True, "reason": "event_deleted"}
        with store.db() as db:
            assert db.execute("SELECT deleted FROM core_events WHERE id=?", (original.id,)).fetchone()[0] == 1
    else:
        corrected = replace(original, text="用户已更正原话") if change == "text" else replace(original, conversation="corrected-room")
        expected = events.ingest(store, corrected)
        with pytest.raises(AssistantError) as error:
            await core.handle(queued)
        assert error.value.code == "context_changed"
        assert events.get(store, original.id)["version"] == expected["version"]
        assert events.get(store, original.id)["event"] == corrected.as_dict() | {"attachments": []}
    assert not model.calls and not adapter.sent
    assert store.records("conversation_turns")[0] == []


@pytest.mark.asyncio
async def test_poll_can_ingest_and_consume_latest_platform_correction(setup):
    store, core, model, adapter, _ = setup
    original = event()
    adapter.incoming = [original]
    assert await core.poll() == [original]
    corrected = replace(original, text="平台返回的最新更正", conversation="corrected-room")
    adapter.incoming = [corrected]
    incoming = await core.poll()
    assert incoming == [corrected]
    assert (await core.handle(incoming[0]))["action"]["status"] == "succeeded"
    assert model.calls[-1]["event"]["text"] == corrected.text
    assert len(adapter.sent) == 1
    assert store.state("core-processed:" + corrected.id) == events.get(store, corrected.id)["version"]
    assert await core.poll() == []
