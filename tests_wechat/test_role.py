"""Role admission using synthetic source observations only; no live UI or model."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from filelock import FileLock

from companion_core import events
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.storage import Store
from companion_wechat.contracts import SendRequest
from companion_wechat.receiver import DatabaseGateway
from companion_wechat.role import RoleAdapter, RoleSession
from tests_wechat.test_receiver import GROUP, OWNER, STAMP, Factory, Worker, row, snapshot

ACCOUNT = "synthetic-role-account"
GENERATION = "7" * 32


class Core:
    run_key = "core-run:wechat"

    def __init__(self, store, session):
        self.store, self.session = store, session

    def guard(self):
        run = self.store.state(self.run_key, {})
        if run.get("status") != "active":
            raise AssistantError("not_running", "synthetic stop")
        self.session.validate_run(run)
        return run


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "synthetic")
    assert store.initialize()["ok"]
    receiver = DatabaseGateway(store, {}, account=ACCOUNT, transport_factory=Factory(Worker([snapshot()])))
    await receiver.start()
    clock = [datetime.fromtimestamp(STAMP, timezone.utc)]
    session = RoleSession(store, ACCOUNT, clock=lambda: clock[0], nonce_factory=lambda: "A" * 24)
    core = Core(store, session)
    session.bind(receiver, core)
    session.prepare()
    store.set_state(core.run_key, {"status": "active", "generation": GENERATION, "owner": "synthetic-control",
                                 "accounts": [["wechat", ACCOUNT]], "read_only": False})
    core.guard()
    yield SimpleNamespace(store=store, receiver=receiver, session=session, core=core, clock=clock)
    await receiver.close()
    session.close()


async def incoming(env, number=1, *, group=False, sender=OWNER, text=None, fresh=True, occurred=None):
    data = row(number, group=group, sender=sender, text=text if text is not None else env.session.nonce + " test")
    if occurred is not None:
        data["occurred_at"] = occurred
    observation = env.receiver._observations([data])[0]
    env.receiver.inbox.publish(observation)
    batch, _cursor = await env.receiver.connector.receive({})
    event = next(value for value in batch if value.native_id == observation.observation_id)
    events.ingest(env.store, event)
    with env.store.lock, env.store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        env.session.admit([event], fresh_ids={event.id} if fresh else set(), db=db)
    return event


def request(env, event, action_id="synthetic-action", *, text="synthetic reply"):
    binding = env.receiver.bindings.get(event.conversation)
    with env.store.db() as db:
        db.execute("INSERT INTO core_actions VALUES (?,?,?,?,?,?,?,?,?,?)", (
            action_id, stable_key("dispatch", "wechat", ACCOUNT, event.id, "message.reply"), GENERATION,
            "wechat", ACCOUNT, "message.reply", env.store.ledger_encode("core_actions.body", {
                "event": event.as_dict(), "arguments": {"text": text}}), "dispatching", None,
            env.clock[0].isoformat()))
    return SendRequest(action_id, GENERATION, env.receiver.bindings.identity, env.receiver.inbox.epoch,
                       binding.key, binding.revision, binding.target_ref, text)


def test_status_contains_only_nonce_time_and_target_counts(env):
    status = env.session.public_status()
    assert set(status) == {"status", "nonce", "deadline_at", "admitted", "reserved", "completed"}
    assert status["nonce"] == "XLD-ROLE-" + "A" * 24 and status["status"] == "active"
    assert OWNER not in json.dumps(status) and GROUP not in json.dumps(status)
    assert repr(env.session) == "RoleSession(<redacted>)"


def test_remaining_seconds_is_read_only_and_never_calls_core_guard(env):
    original = env.store.state(env.session.key)
    env.core.guard = lambda: pytest.fail("remaining_seconds must not invoke core guards")
    assert env.session.remaining_seconds() == 180.0
    env.clock[0] += timedelta(seconds=20.5)
    assert env.session.remaining_seconds() == 159.5
    env.clock[0] += timedelta(seconds=160)
    assert env.session.remaining_seconds() == 0.0
    assert env.store.state(env.session.key) == original
    env.session.close()
    assert env.session.remaining_seconds() == 0.0


def test_unprepared_remaining_seconds_is_zero(tmp_path):
    store = Store(tmp_path / "synthetic-unprepared")
    assert store.initialize()["ok"]
    assert RoleSession(store, ACCOUNT).remaining_seconds() == 0.0


@pytest.mark.asyncio
async def test_owner_private_and_ordinary_group_are_admitted_without_fabricating_trigger(env):
    private = await incoming(env)
    group = await incoming(env, group=True)
    assert group.metadata["wechat"]["trigger"] == "none"
    assert env.session.admitted_pending() == [private, group]
    env.session.check_event(private)
    env.session.check_event(group)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no_nonce", "embedded_nonce", "old_time", "future_time", "other_sender", "existing"])
async def test_unapproved_messages_never_enter_admitted_pending(env, kind):
    options = {"group": True}
    if kind == "no_nonce":
        options["text"] = "ordinary family message"
    elif kind == "embedded_nonce":
        options["text"] = "quote " + env.session.nonce
    elif kind == "old_time":
        options["occurred"] = (env.clock[0] - timedelta(seconds=1)).isoformat()
    elif kind == "future_time":
        options["occurred"] = (env.clock[0] + timedelta(seconds=31)).isoformat()
    elif kind == "other_sender":
        options["sender"] = "synthetic-other-member"
    else:
        options["fresh"] = False
    event = await incoming(env, **options)
    assert env.session.admitted_pending() == []
    with pytest.raises(AssistantError) as exc:
        env.session.check_event(event)
    assert exc.value.code == "wechat_role_not_admitted"


@pytest.mark.asyncio
async def test_only_first_matching_event_per_target_can_be_admitted(env):
    first = await incoming(env)
    second = await incoming(env, 2)
    assert env.session.admitted_pending() == [first]
    with pytest.raises(AssistantError, match="边界"):
        env.session.check_event(second)
    assert env.session.public_status()["admitted"] == {"owner_main": 1, "family": 0}


@pytest.mark.asyncio
async def test_admission_rechecks_source_version_and_tombstone(env):
    event = await incoming(env)
    with env.store.db() as db:
        db.execute("UPDATE core_events SET deleted=1 WHERE id=?", (event.id,))
    with pytest.raises(AssistantError):
        env.session.check_event(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deadline", "generation", "stopped", "epoch", "binding", "revoked"])
async def test_session_is_revalidated_before_model_or_send(env, change):
    event = await incoming(env)
    if change == "deadline":
        env.clock[0] += timedelta(seconds=180)
    elif change in {"generation", "stopped"}:
        run = env.store.state(env.core.run_key)
        run["generation" if change == "generation" else "status"] = "another" if change == "generation" else "paused"
        env.store.set_state(env.core.run_key, run)
    elif change == "epoch":
        env.receiver.inbox.epoch = "different"
    elif change == "binding":
        binding = env.receiver.bindings.get("owner_main")
        env.receiver.bindings.bind(replace(binding, enabled=False), expected_revision=binding.revision)
    else:
        env.session.revoke_generation(GENERATION)
    with pytest.raises(AssistantError):
        env.session.check_event(event)


@pytest.mark.asyncio
async def test_check_request_is_repeatable_and_reservation_is_atomic_idempotent(env):
    event = await incoming(env)
    value = request(env, event)
    for _ in range(3):
        env.session.check_request(value)
    assert env.session.public_status()["reserved"]["owner_main"] == 0
    with pytest.raises(RuntimeError), env.store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        env.session.reserve(value, db=db)
        raise RuntimeError("synthetic intent transaction rollback")
    assert env.session.public_status()["reserved"]["owner_main"] == 0
    env.session.reserve(value)
    env.session.reserve(value)
    env.session.check_request(value)
    assert env.session.public_status()["reserved"]["owner_main"] == 1
    with env.store.db() as db:
        db.execute("UPDATE core_actions SET status='succeeded' WHERE id=?", (value.action_id,))
    assert env.session.public_status()["completed"]["owner_main"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("target_ref", "wrong"), ("revision", 99), ("generation", "old"),
                                         ("text", "changed"), ("action_id", "missing")])
async def test_send_request_cannot_change_the_frozen_core_action(env, field, value):
    event = await incoming(env)
    initial = request(env, event)
    with pytest.raises(AssistantError):
        env.session.check_request(replace(initial, **{field: value}))


@pytest.mark.asyncio
async def test_new_action_id_cannot_reuse_reserved_target(env):
    event = await incoming(env)
    initial = request(env, event)
    env.session.reserve(initial)
    with env.store.db() as db:
        db.execute("UPDATE core_actions SET dedupe=dedupe || ':retired' WHERE id=?", (initial.action_id,))
    retry = request(env, event, "another-attempt")
    with pytest.raises(AssistantError) as exc:
        env.session.check_request(retry)
    assert exc.value.code == "wechat_role_reply_limit"


def test_existing_collector_lock_cannot_be_stolen(tmp_path):
    store = Store(tmp_path / "synthetic-lock")
    store.initialize()
    session = RoleSession(store, ACCOUNT)
    session.core = SimpleNamespace()
    external = FileLock(str(store.runtime / (stable_key("wechat-collector", ACCOUNT) + ".lock")), timeout=0)
    with external, pytest.raises(AssistantError) as exc:
        session.prepare()
    assert exc.value.code == "wechat_role_collector_busy"


def test_other_platform_run_is_allowed_but_legacy_wechat_run_is_not(env):
    env.store.set_state("core-run", {"status": "active", "accounts": [["bilibili", "synthetic-bili"]]})
    env.core.guard()
    env.store.set_state("core-run", {"status": "active", "accounts": [["wechat", ACCOUNT]]})
    with pytest.raises(AssistantError) as exc:
        env.core.guard()
    assert exc.value.code == "wechat_role_already_active"


def test_prepare_preserves_an_existing_legacy_wechat_run(env):
    env.session.close()
    env.store.set_state(env.core.run_key, {"status": "paused"})
    active = {"status": "active", "accounts": [["wechat", ACCOUNT]], "generation": "legacy-run"}
    env.store.set_state("core-run", active)
    session = RoleSession(env.store, ACCOUNT)
    session.bind(env.receiver, Core(env.store, session))
    with pytest.raises(AssistantError) as exc:
        session.prepare()
    assert exc.value.code == "wechat_role_already_active"
    assert not session._lease.is_locked and session._id is None
    assert env.store.state("core-run") == active


@pytest.mark.asyncio
async def test_processed_admission_is_not_returned_and_close_revokes_and_releases(env):
    event = await incoming(env)
    version = events.get(env.store, event.id)["version"]
    env.store.set_state("core-processed:" + event.id, version)
    assert env.session.admitted_pending() == []
    env.session.close()
    env.session.close()
    assert not env.session._lease.is_locked and env.session.public_status()["status"] == "revoked"


def test_short_test_capability_is_single_line_and_bounded():
    adapter = object.__new__(RoleAdapter)
    value = adapter.capabilities()[0]
    assert value.name == "message.reply"
    assert value.parameters["properties"]["text"]["maxLength"] == 600


def test_missing_sender_preflight_cannot_prepare_session():
    adapter = object.__new__(RoleAdapter)
    prepared = []

    def denied():
        raise AssistantError("wechat_ui_binding_required", "synthetic")

    adapter.sender = SimpleNamespace(preflight=denied)
    adapter.session = SimpleNamespace(prepare=lambda: prepared.append(True))
    with pytest.raises(AssistantError):
        adapter.validate_start()
    assert not prepared


@pytest.mark.asyncio
async def test_close_releases_every_resource_even_if_revocation_and_sender_cleanup_fail():
    adapter = object.__new__(RoleAdapter)
    calls = []

    def revoke(_generation):
        calls.append("revoke")
        raise RuntimeError("synthetic revoke failure")

    async def sender_close():
        calls.append("sender")
        raise RuntimeError("synthetic sender cleanup failure")

    async def receiver_close():
        calls.append("receiver")

    adapter.runtime_stopped = revoke
    adapter.sender = SimpleNamespace(close=sender_close)
    adapter.receiver = SimpleNamespace(close=receiver_close)
    adapter.session = SimpleNamespace(_generation=None, close=lambda: calls.append("session"))
    with pytest.raises(RuntimeError):
        await adapter.close()
    assert calls == ["revoke", "sender", "receiver", "session"]
    await adapter.close()
    assert calls == ["revoke", "sender", "receiver", "session"]


@pytest.mark.asyncio
async def test_close_is_idempotent_after_successful_cleanup():
    adapter = object.__new__(RoleAdapter)
    calls = []

    async def sender_close():
        calls.append("sender")

    async def receiver_close():
        calls.append("receiver")

    adapter.runtime_stopped = lambda _generation: calls.append("revoke")
    adapter.sender = SimpleNamespace(close=sender_close)
    adapter.receiver = SimpleNamespace(close=receiver_close)
    adapter.session = SimpleNamespace(_generation=None, close=lambda: calls.append("session"))
    await adapter.close()
    await adapter.close()
    assert calls == ["revoke", "sender", "receiver", "session"]
