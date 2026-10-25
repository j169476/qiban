"""Synthetic producer failures. Never opens WeChat or an external database."""
import asyncio
import copy
import hashlib
from collections import deque
from datetime import datetime, timezone

import pytest

from companion_core.errors import AssistantError
from companion_core.storage import Store
from companion_wechat.receiver import DatabaseGateway
from companion_wechat.receiver_transport import WorkerError

STAMP = 1792814400
SELF, OWNER, GROUP = "synthetic_bot", "synthetic_owner", "synthetic_family@chatroom"


def snapshot():
    return {"enrollment": {
        "schema_version": 1, "epoch": "b" * 32,
        "account_id": "wechat-db:" + hashlib.sha256(SELF.encode()).hexdigest(),
        "account_directory_id": "c" * 64, "self_username": SELF, "since": STAMP,
        "targets": {"owner_main": {"kind": "private", "username": OWNER},
                    "family": {"kind": "group", "username": GROUP}}},
        "checkpoint": {"schema_version": 1, "epoch": "b" * 32, "sources": {
            f"message/message_0.db|{alias}": {"shard": "message/message_0.db", "target_ref": alias,
                                         "salt": "d" * 64, "anchor": None}
            for alias in ("owner_main", "family")}}}


def row(number, *, group=False, sender=OWNER, text="synthetic identical bubbles"):
    return {"target_ref": GROUP if group else OWNER, "kind": "group" if group else "private",
            "server_id": str(number), "sender_ref": sender, "text": text,
            "occurred_at": datetime.fromtimestamp(STAMP + number, timezone.utc).isoformat(),
            "direction": "self" if sender == SELF else "incoming",
            "trigger": "none" if group else "direct", "time_basis": "server"}


def batch(before, *rows, skipped=0, blocked=None):
    after = copy.deepcopy(before)
    for item in rows:
        alias = "owner_main" if item["kind"] == "private" else "family"
        number = int(item["server_id"])
        after["checkpoint"]["sources"]["message/message_0.db|" + alias]["anchor"] = {
            "local_id": number, "server_id": number, "create_time": STAMP + number}
    return {"rows": list(rows), "next_checkpoint": after["checkpoint"],
            "skipped": skipped, "blocked": blocked or []}, after


class Worker:
    def __init__(self, responses):
        self.responses, self.calls = deque(responses), []
        self.closed = False

    async def request(self, command, **arguments):
        self.calls.append((command, copy.deepcopy(arguments)))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    async def close(self):
        self.closed = True


class Factory:
    def __init__(self, *workers):
        self.workers, self.created = deque(workers), []

    def __call__(self):
        worker = self.workers.popleft()
        self.created.append(worker)
        return worker


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "synthetic")
    assert value.initialize()["ok"]
    return value


@pytest.mark.asyncio
async def test_two_targets_identical_text_bubbles_restart_replay_and_ack(store):
    first = snapshot()
    data, after = batch(first, row(1), row(2), row(1, group=True), row(2, group=True))
    worker = Worker([first, data])
    gateway = DatabaseGateway(store, {}, transport_factory=Factory(worker))
    await gateway.start()
    assert gateway.connector.capabilities() == []
    await gateway.pump_once()
    events, cursor = await gateway.connector.receive({})
    assert len(events) == 4 and len({e.id for e in events}) == 4
    assert {e.text for e in events} == {"synthetic identical bubbles"}
    assert {e.conversation for e in events} == {"owner_main", "family"}
    assert all(e.metadata["wechat"]["trigger"] == "none" for e in events if e.visibility == "group")
    epoch = gateway.connection().epoch
    await gateway.close()
    assert worker.closed

    # New process-equivalent Store and gateway resume the committed source,
    # preserving the outstanding batch and its normalization fingerprints.
    resumed_store = Store(store.root)
    repeated = {**data, "next_checkpoint": after["checkpoint"]}
    resumed_worker = Worker([after, repeated])
    resumed = DatabaseGateway(resumed_store, {}, transport_factory=Factory(resumed_worker))
    await resumed.start()
    assert resumed_worker.calls[0][1]["checkpoint"] == after["checkpoint"]
    assert resumed.connection().epoch == epoch
    replay, replay_cursor = await resumed.connector.receive({})
    assert [e.as_dict() for e in replay] == [e.as_dict() for e in events]
    assert replay_cursor == cursor
    assert (await resumed.connector.receive(cursor))[0] == []
    await resumed.pump_once()  # Already acknowledged server IDs never resurrect.
    assert (await resumed.connector.receive(cursor))[0] == []
    denied = await resumed.connector.execute("message.reply", {}, guard=lambda: None)
    assert denied.status == "cancelled" and denied.error == "wechat_read_only"
    await resumed.close()


@pytest.mark.asyncio
async def test_disconnection_backoff_and_offline_pending_queue_drain(store):
    before = snapshot()
    data, after = batch(before, row(1))
    empty, _ = batch(after)
    first = Worker([before, data, WorkerError("client_not_running")])
    second = Worker([after, empty])
    factory = Factory(first, second)
    now = [0.0]
    gateway = DatabaseGateway(store, {}, transport_factory=factory, clock=lambda: now[0])
    await gateway.start()
    await gateway.pump_once()
    assert (await gateway.pump_once())["state"] == "retrying"
    assert first.closed
    events, cursor = await gateway.connector.receive({})
    assert len(events) == 1  # Native client unavailable, durable delivery works.
    assert (await gateway.pump_once())["retry_in_seconds"] == 1
    assert len(factory.created) == 1
    now[0] = 1
    assert (await gateway.pump_once())["state"] == "connected"
    assert second.calls[0][1]["checkpoint"] == after["checkpoint"]
    assert (await gateway.connector.receive(cursor))[0] == []
    await gateway.close()


@pytest.mark.asyncio
async def test_restart_while_client_offline_keeps_bindings_and_cursor(store):
    first = snapshot()
    data, after = batch(first, row(1))
    gateway = DatabaseGateway(store, {}, transport_factory=Factory(Worker([first, data])))
    await gateway.start()
    await gateway.pump_once()
    await gateway.close()
    retry = DatabaseGateway(store, {}, transport_factory=Factory(Worker([WorkerError("client_not_running")])))
    status = await retry.start()
    assert status["state"] == "retrying"
    assert len((await retry.connector.receive({}))[0]) == 1
    assert retry.inbox.checkpoint(retry.source_key) == after
    await retry.close()


@pytest.mark.asyncio
async def test_read_failure_after_successful_open_keeps_exponential_backoff(store):
    initial = snapshot()
    workers = [Worker([initial, WorkerError("worker_timeout")]) for _ in range(6)]
    factory = Factory(*workers)
    now = [0.0]
    gateway = DatabaseGateway(store, {}, transport_factory=factory, clock=lambda: now[0], retry_max=8)
    await gateway.start()
    for delay in (1, 2, 4, 8, 8, 8):
        status = await gateway.pump_once()
        assert status["retry_in_seconds"] == delay
        now[0] += delay
    await gateway.close()


@pytest.mark.asyncio
async def test_full_queue_does_not_advance_source_checkpoint(store):
    initial = snapshot()
    one, after_one = batch(initial, row(1))
    two, after_two = batch(after_one, row(2))
    worker = Worker([initial, one, two, two])
    gateway = DatabaseGateway(store, {}, capacity=1, transport_factory=Factory(worker))
    await gateway.start()
    await gateway.pump_once()
    assert (await gateway.pump_once())["state"] == "backpressure"
    assert gateway.inbox.checkpoint(gateway.source_key) == after_one
    events, cursor = await gateway.connector.receive({})
    assert len(events) == 1
    await gateway.connector.receive(cursor)
    await gateway.pump_once()
    assert gateway.inbox.checkpoint(gateway.source_key) == after_two
    assert len((await gateway.connector.receive(cursor))[0]) == 1
    await gateway.close()


@pytest.mark.asyncio
async def test_failed_atomic_publish_keeps_source_for_restart(store, monkeypatch):
    initial = snapshot()
    data, after = batch(initial, row(1), row(2))
    gateway = DatabaseGateway(store, {}, transport_factory=Factory(Worker([initial, data])))
    await gateway.start()
    original = store.set_state

    def fail_checkpoint(key, value, db=None):
        if ":checkpoint:" in key:
            raise RuntimeError("synthetic private detail must not escape")
        return original(key, value, db)

    with monkeypatch.context() as m:
        m.setattr(store, "set_state", fail_checkpoint)
        with pytest.raises(AssistantError, match="已保留原游标") as error:
            await gateway.pump_once()
        assert error.value.code == "wechat_receiver_failed"
    assert gateway.inbox.checkpoint(gateway.source_key) == initial
    assert (await gateway.connector.receive({}))[0] == []
    await gateway.close()
    retry = DatabaseGateway(store, {}, transport_factory=Factory(Worker([initial, data])))
    await retry.start()
    await retry.pump_once()
    assert retry.inbox.checkpoint(retry.source_key) == after
    assert len((await retry.connector.receive({}))[0]) == 2
    await retry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["source_anchor_changed", "source_generation_changed", "client_version_unsupported"])
async def test_permanent_source_change_blocks_without_rebaseline(store, code):
    initial = snapshot()
    factory = Factory(Worker([initial, WorkerError(code)]))
    gateway = DatabaseGateway(store, {}, transport_factory=factory)
    await gateway.start()
    assert (await gateway.pump_once())["state"] == "blocked"
    assert (await gateway.pump_once())["code"] == code
    assert len(factory.created) == 1 and gateway.inbox.checkpoint(gateway.source_key) == initial
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    ({"target_ref": "synthetic_unapproved"}, "wechat_target_unbound"),
    ({"sender_ref": "synthetic_stranger"}, "wechat_sender_mismatch"),
    ({"server_id": "0"}, "wechat_source_invalid"),
    ({"trigger": "mention"}, "wechat_source_invalid"),
])
async def test_scope_violation_never_commits(store, change, code):
    initial = snapshot()
    data, _ = batch(initial, row(1))
    data["rows"][0].update(change)
    gateway = DatabaseGateway(store, {}, transport_factory=Factory(Worker([initial, data])))
    await gateway.start()
    with pytest.raises(AssistantError) as error:
        await gateway.pump_once()
    assert error.value.code == code
    assert gateway.inbox.checkpoint(gateway.source_key) == initial
    assert (await gateway.connector.receive({}))[0] == []
    await gateway.close()


@pytest.mark.asyncio
async def test_self_excluded_and_database_resolved_group_member_is_verified(store):
    initial = snapshot()
    data, _ = batch(initial, row(1, sender=SELF), row(2, group=True, sender="synthetic_member"))
    gateway = DatabaseGateway(store, {}, transport_factory=Factory(Worker([initial, data])))
    await gateway.start()
    await gateway.pump_once()
    events, _ = await gateway.connector.receive({})
    assert len(events) == 1 and events[0].visibility == "group"
    assert events[0].metadata["wechat"]["identity_quality"] == "verified"
    assert events[0].actor.scope == gateway.account
    await gateway.close()


@pytest.mark.asyncio
async def test_first_pointer_survives_crash_before_queue_installation(store, monkeypatch):
    initial = snapshot()
    gateway = DatabaseGateway(store, {}, transport_factory=Factory(Worker([initial])))
    with monkeypatch.context() as m:
        m.setattr(gateway, "_initialize", lambda pointer: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            await gateway.start()
    assert store.state(gateway.pointer_key) == initial
    worker = Worker([initial])
    resumed = DatabaseGateway(store, {}, transport_factory=Factory(worker))
    await resumed.start()
    assert worker.calls[0][1]["checkpoint"] == initial["checkpoint"]
    await resumed.close()


@pytest.mark.asyncio
async def test_one_logical_consumer_per_verified_identity(store):
    initial = snapshot()
    first = DatabaseGateway(store, {}, account="synthetic-a", transport_factory=Factory(Worker([initial])))
    second = DatabaseGateway(store, {}, account="synthetic-b", transport_factory=Factory(Worker([initial])))
    await first.start()
    with pytest.raises(AssistantError) as error:
        await second.start()
    assert error.value.code == "wechat_receiver_owner_conflict"
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_close_discards_late_read_reply_before_commit(store):
    initial = snapshot()
    data, _ = batch(initial, row(1))
    entered, release = asyncio.Event(), asyncio.Event()

    class LateWorker(Worker):
        async def request(self, command, **arguments):
            if command == "read":
                entered.set()
                await release.wait()
            return await super().request(command, **arguments)

    worker = LateWorker([initial, data])
    gateway = DatabaseGateway(store, {}, transport_factory=Factory(worker))
    await gateway.start()
    pump = asyncio.create_task(gateway.pump_once())
    await entered.wait()
    await gateway.close()
    release.set()
    assert (await pump)["state"] == "closed"
    assert gateway.inbox.checkpoint(gateway.source_key) == initial
    assert (await gateway.inbox.poll({})).observations == ()
