import ast
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_core import events
from companion_core.contracts import Event, stable_key
from companion_core.errors import AssistantError
from companion_core.runtime import Companion
from companion_core.storage import Store
from companion_wechat.bindings import BindingRegistry, ConversationBinding
from companion_wechat.connector import WeChatConnector
from companion_wechat.contracts import Connection, Observation
from companion_wechat.gateway import PermitAuthority
from companion_wechat.providers.fake import FakeGateway

NOW = "2026-10-24T04:00:00+00:00"


class Host:
    def identity(self):
        return "synthetic-wechat-test"

    def require_active(self, thread_id=None):
        pass


class Model:
    async def generate(self, instructions, context, output_schema, images=(), **options):
        return {"note": "", "action": {"capability": "message.reply", "arguments": {"text": "synthetic reply"}},
                "memories": [], "identity_proposals": []}


def observation(**values):
    return Observation(**{"observation_id": uuid.uuid4().hex, "account_identity": "synthetic-self",
                          "epoch": "synthetic-epoch", "target_ref": "synthetic-peer-target", "kind": "private",
                          "occurred_at": NOW, "text": "synthetic input", "sender_ref": "verified-peer",
                          "trigger": "direct", **values})


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "synthetic")
    assert store.initialize()["ok"]
    bindings = BindingRegistry(store, "synthetic-account", "synthetic-self")
    private = bindings.bind(ConversationBinding("private", "synthetic-peer-target", "private", "verified-peer"))
    group = bindings.bind(ConversationBinding("group", "synthetic-group-target", "group"))
    authority = PermitAuthority(b"synthetic-test-key-not-for-live!!" * 2)
    gateway = FakeGateway("synthetic-self", authority)
    gateway.bind_target(private)
    gateway.bind_target(group)
    connector = WeChatConnector(bindings, gateway, authority, allow_simulated_send=True)
    core = Companion(store, Model(), Host(), [connector], clock=lambda: datetime.fromisoformat(NOW))
    core.start()
    return SimpleNamespace(store=store, bindings=bindings, private=private, group=group,
                           permits=authority, gateway=gateway, connector=connector, core=core)


async def incoming(env, item=None):
    env.gateway.publish(item or observation())
    return (await env.core.poll())[0]


async def dispatch(env, event, **arguments):
    return await env.core.dispatch(event, {"capability": "message.reply",
                                          "arguments": {"text": "synthetic reply", **arguments}})


@pytest.mark.asyncio
async def test_same_text_twice_preserved_and_exact_replay_idempotent(env):
    first, second = observation(), observation()
    for item in (first, first, second):
        env.gateway.publish(item)
    found = await env.core.poll()
    assert len(found) == 2 and found[0].text == found[1].text
    assert len({event.id for event in found}) == 2
    assert env.gateway.acknowledged == 0
    assert len(await env.core.poll()) == 2  # durable pending inbox
    assert env.gateway.acknowledged == 3 and not env.gateway.inbox


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"text": "changed"}, {"sender_ref": "someone-else"}, {"target_ref": "elsewhere"},
    {"kind": "group"}, {"occurred_at": "2026-10-24T04:01:00+00:00"},
    {"epoch": "another-epoch"}, {"account_identity": "different-account"}, {"trigger": "mention"},
])
async def test_observation_collision_rejected_after_connector_recreation(env, change):
    item = observation()
    first = await incoming(env, item)
    original = events.get(env.store, first.id)
    # Simulate a replay while the first cursor has not yet been confirmed.
    env.gateway.publish(replace(item, **change))
    rebuilt = WeChatConnector(env.bindings, env.gateway, env.permits)
    with pytest.raises(AssistantError):
        await rebuilt.receive({"since": NOW})
    assert events.get(env.store, first.id) == original
    assert env.gateway.acknowledged == 0


@pytest.mark.asyncio
async def test_deleted_event_is_not_resurrected_or_accepted_with_changed_payload(env):
    item = observation()
    event = await incoming(env, item)
    events.forget(env.store, event.id)
    replay, _ = await env.connector.receive({"since": NOW})
    assert replay == []
    env.gateway.publish(replace(item, text="changed"))
    with pytest.raises(AssistantError, match="边界"):
        await env.connector.receive({"since": NOW})
    with env.store.db() as db:
        assert db.execute("SELECT deleted FROM core_events WHERE id=?", (event.id,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_invalid_batch_reservations_rollback_atomically(env):
    item = observation()
    env.gateway.publish(item)
    env.gateway.publish(replace(item, text="conflict in same batch"))
    with pytest.raises(AssistantError):
        await env.core.poll()
    assert not env.store.states("wechat-observation-v1-%")
    assert env.gateway.acknowledged == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["self", "system", "history"])
async def test_echo_system_and_initial_history_never_enter_core_events(env, direction):
    env.gateway.publish(observation(direction=direction))
    assert await env.core.poll() == []
    await env.core.poll()
    assert env.gateway.acknowledged == 1
    with env.store.db() as db:
        assert db.execute("SELECT count(*) FROM core_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_group_requires_trigger_and_never_merges_nicknames(env):
    for trigger in ("none", "mention", "reply"):
        env.gateway.publish(observation(kind="group", target_ref=env.group.target_ref,
                                        sender_ref="same-display-name", trigger=trigger))
    found = await env.core.poll()
    assert len(found) == 2
    assert found[0].actor != found[1].actor
    assert found[0].thread == found[1].thread
    assert all(e.visibility == "group" and e.metadata["wechat"]["identity_quality"] == "observation_only"
               for e in found)


@pytest.mark.asyncio
async def test_group_verified_member_and_private_peer_share_only_account_scoped_actor(env):
    env.bindings.bind(replace(env.group, members={"verified-member-ref": "verified-peer"}), expected_revision=1)
    private = await incoming(env)
    env.gateway.publish(observation(kind="group", target_ref=env.group.target_ref,
                                    sender_ref="verified-member-ref", trigger="mention"))
    found = await env.core.poll()
    group = next(e for e in found if e.visibility == "group")
    assert group.actor == private.actor and group.thread != private.thread


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"sender_ref": "wrong-peer"}, {"kind": "group"}, {"target_ref": "not-allowed"},
    {"account_identity": "wrong-account"}, {"epoch": "old-connection"},
])
async def test_untrusted_inbound_never_reaches_database(env, change):
    env.gateway.publish(observation(**change))
    with pytest.raises(AssistantError):
        await env.core.poll()
    with env.store.db() as db:
        assert db.execute("SELECT count(*) FROM core_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_partial_core_commit_replays_without_acking_or_source_change(env, monkeypatch):
    items = [observation(), observation()]
    for item in items:
        env.gateway.publish(item)
    original = events.ingest
    seen = 0

    def fail_second(store, event):
        nonlocal seen
        seen += 1
        if seen == 2:
            raise OSError("synthetic database failure")
        return original(store, event)

    monkeypatch.setattr(events, "ingest", fail_second)
    with pytest.raises(OSError):
        await env.core.poll()
    assert env.gateway.acknowledged == 0
    with env.store.db() as db:
        row = db.execute("SELECT id,version FROM core_events").fetchone()
    monkeypatch.setattr(events, "ingest", original)
    assert len(await env.core.poll()) == 2
    assert events.get(env.store, row["id"])["version"] == row["version"]
    assert env.gateway.acknowledged == 0
    await env.core.poll()
    assert env.gateway.acknowledged == 2


@pytest.mark.asyncio
async def test_failed_cursor_commit_preserves_batch_for_replay(env, monkeypatch):
    env.gateway.publish(observation())
    original = env.store.set_state
    cursor_key = stable_key("cursor", "wechat", env.connector.account)

    def fail_cursor(key, value, db=None):
        if key == cursor_key:
            raise OSError("synthetic cursor failure")
        return original(key, value, db)

    monkeypatch.setattr(env.store, "set_state", fail_cursor)
    with pytest.raises(OSError):
        await env.core.poll()
    assert env.gateway.acknowledged == 0
    monkeypatch.setattr(env.store, "set_state", original)
    assert len(await env.core.poll()) == 1
    await env.core.poll()
    assert env.gateway.acknowledged == 1


@pytest.mark.asyncio
async def test_poll_handle_uses_core_action_id_and_only_frozen_target(env):
    event = await incoming(env)
    result = await env.core.handle(event)
    action = result["action"]
    assert action["status"] == "succeeded"
    assert env.gateway.sent[0].action_id == action["id"]
    assert env.gateway.sent[0].target_ref == env.private.target_ref
    assert env.gateway.sent[0].generation == env.core.guard()["generation"]
    assert (await env.core.handle(event))["duplicate"]
    assert len(env.gateway.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stop", "binding", "account", "epoch", "delete", "manual"])
async def test_change_during_window_preparation_cancels_before_send(env, change):
    event = await incoming(env)
    called = []

    def alter():
        called.append(True)
        if change == "stop":
            env.core.stop()
        elif change == "binding":
            env.bindings.bind(replace(env.private, enabled=False), expected_revision=1)
        elif change == "account":
            env.gateway.session = Connection("wrong-account", "synthetic-epoch")
        elif change == "epoch":
            env.gateway.session = Connection("synthetic-self", "new-connection")
        elif change == "delete":
            events.forget(env.store, event.id)
        else:
            env.gateway.paused_targets.add(env.private.target_ref)

    env.gateway.after_prepare = alter
    result = await dispatch(env, event)
    assert called == [True]
    assert result["status"] == "cancelled"
    assert not env.gateway.sent and not env.gateway.pending


@pytest.mark.asyncio
async def test_stop_after_permit_before_commit_revokes_generation(env):
    event = await incoming(env)
    env.gateway.before_commit = env.core.stop
    assert (await dispatch(env, event))["status"] == "cancelled"
    assert not env.gateway.sent


@pytest.mark.asyncio
async def test_stop_after_submission_does_not_claim_send_was_cancelled(env):
    event = await incoming(env)
    env.gateway.after_submit = env.core.stop
    assert (await dispatch(env, event))["status"] == "succeeded"
    assert len(env.gateway.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout_after", "click_only"])
async def test_unknown_result_is_not_resent_after_core_restart(env, outcome):
    event = await incoming(env)
    env.gateway.outcome = outcome
    assert (await dispatch(env, event))["status"] == "uncertain"
    env.core.stop()
    rebuilt = WeChatConnector(env.bindings, env.gateway, env.permits, allow_simulated_send=True)
    core = Companion(env.store, Model(), Host(), [rebuilt])
    core.start()
    again = await core.dispatch(event, {"capability": "message.reply", "arguments": {"text": "synthetic reply"}})
    assert again["status"] == "uncertain" and again["duplicate"]
    assert len(env.gateway.sent) == 1


@pytest.mark.asyncio
async def test_default_connector_cannot_send_even_to_simulator(env):
    event = await incoming(env)
    env.connector.allow_simulated_send = False
    result = await dispatch(env, event)
    assert result["status"] == "cancelled" and result["error"] == "wechat_live_send_unavailable"
    assert not env.gateway.sent


@pytest.mark.asyncio
async def test_real_gateway_cannot_be_enabled_in_p1(env):
    event = await incoming(env)
    env.gateway.simulation = False
    assert (await dispatch(env, event))["error"] == "wechat_live_send_unavailable"
    assert not env.gateway.sent


@pytest.mark.asyncio
async def test_fabricated_event_cannot_bypass_receive(env):
    event = await incoming(env)
    forged = Event.from_dict({**event.as_dict(), "text": "fabricated input"})
    assert (await dispatch(env, forged))["error"] == "wechat_event_untrusted"
    assert not env.gateway.sent


@pytest.mark.asyncio
async def test_extra_model_recipient_is_rejected(env):
    from jsonschema.exceptions import ValidationError

    event = await incoming(env)
    with pytest.raises(ValidationError):
        await dispatch(env, event, recipient="someone-else")
    assert not env.gateway.sent


@pytest.mark.asyncio
async def test_read_only_run_observes_but_cannot_dispatch(env):
    env.core.stop()
    env.core.start(read_only=True)
    event = await incoming(env)
    with pytest.raises(AssistantError) as error:
        await dispatch(env, event)
    assert error.value.code == "action_disabled" and not env.gateway.sent


def test_bindings_are_explicit_cas_and_cannot_reassign_conversation(env):
    with pytest.raises(AssistantError):
        BindingRegistry(env.store, env.bindings.account, "different-self")
    with pytest.raises(AssistantError):
        env.bindings.bind(env.private)
    with pytest.raises(AssistantError):
        env.bindings.bind(replace(env.private, peer_ref="new-person"), expected_revision=1)
    with pytest.raises(AssistantError):
        env.bindings.bind(replace(env.private, key="duplicate-target"))


@pytest.mark.asyncio
async def test_gateway_rejects_cursor_from_another_stream_and_ahead_of_delivery(env):
    for cursor in ({"wechat_stream": "other", "offset": 0},
                   {"wechat_stream": env.gateway.stream, "offset": 100}):
        with pytest.raises(AssistantError):
            await env.connector.receive(cursor)
    assert env.gateway.acknowledged == 0


def test_queue_backpressure_does_not_drop_unconfirmed_observation(env):
    env.gateway.capacity = 1
    first = observation()
    env.gateway.publish(first)
    with pytest.raises(AssistantError):
        env.gateway.publish(observation())
    assert env.gateway.inbox == [(1, first)]


@pytest.mark.parametrize("value", ["2026-10-24T04:00:00", "not-a-date", None])
def test_observation_requires_timezone(value):
    with pytest.raises(AssistantError):
        observation(occurred_at=value)


def test_connector_dependency_direction():
    root = Path(__file__).resolve().parents[1] / "src/companion_wechat"
    for path in root.glob("*.py"):
        if path.name in {"receiver_cli.py", "supervised_cli.py", "__main__.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                       else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            assert not any(m.startswith(("pywinauto", "pyweixin", "bili_assistant", "companion_codex",
                                         "companion_service")) for m in modules)


def test_collector_business_layer_does_not_import_service_or_model_runtime():
    source = Path(__file__).resolve().parents[1] / "src/companion_wechat/collector.py"
    forbidden = ("companion_service", "bili_assistant", "companion_codex", "companion_core.runtime",
                 "companion_core.decision", "companion_core.memory", "companion_core.memory_maintenance")
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        modules = ([node.module or ""] + [f"{node.module}.{name.name}" for name in node.names]
                   if isinstance(node, ast.ImportFrom) else
                   [name.name for name in node.names] if isinstance(node, ast.Import) else [])
        assert not any(module == prefix or module.startswith(prefix + ".")
                       for module in modules for prefix in forbidden)
