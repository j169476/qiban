"""Real gateway/connector/collector flow; only native source transport is fake."""

import json

import pytest

from companion_core.errors import AssistantError
from companion_core.runtime_keys import RUN_KEYS
from companion_wechat.collector import Collector, verify_store
from companion_wechat.connector import WeChatConnector
from companion_wechat.receiver import DatabaseGateway
from tests_postgres.test_wechat_receiver_storage import independent_store
from tests_wechat.test_receiver import Factory, Worker, batch, row, snapshot

_ACCOUNT = "synthetic-pg-receiver-flow"
_BODY = "synthetic identical bubbles"


def four_rows():
    initial = snapshot()
    data, advanced = batch(initial, row(1), row(2), row(1, group=True), row(2, group=True))
    return initial, data, advanced


def flow(store, replies):
    worker = Worker(replies)
    gateway = DatabaseGateway(store, {}, account=_ACCOUNT, transport_factory=Factory(worker))
    return gateway, worker, Collector(store, gateway, _ACCOUNT)


def core_rows(store):
    with store.db() as db:
        return db.execute("SELECT id,body FROM core_events ORDER BY id").fetchall()


def assert_receive_only(store, report):
    assert report["messages_sent"] == report["model_calls"] == 0
    assert report["role_started"] is False and report["receive_only"] is True
    assert all(store.state(key, {}).get("status") != "active" for key in RUN_KEYS)
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM core_actions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_full_pg_flow_same_text_private_and_ordinary_group_resume_without_duplicates(pg_store, monkeypatch):
    verify_store(pg_store)

    def forbidden(*_args, **_kwargs):
        pytest.fail("receive-only flow must not load personal records or memory")

    monkeypatch.setattr(pg_store, "read", forbidden)
    monkeypatch.setattr(pg_store, "records", forbidden)
    initial, data, advanced = four_rows()
    gateway, worker, collector = flow(pg_store, [initial, data])
    emitted = []
    report = await collector.run(once=True, emit=emitted.append)
    assert isinstance(gateway.connector, WeChatConnector)
    assert report["status"] == "complete" and report["events_committed"] == 4
    assert worker.closed and gateway.connector.capabilities() == []
    assert_receive_only(pg_store, report)
    assert _BODY not in json.dumps(emitted)

    stored = core_rows(pg_store)
    assert len(stored) == len({record["id"] for record in stored}) == 4
    assert all(record["body"].startswith("enc1:") and _BODY not in record["body"] for record in stored)
    decoded = [pg_store.ledger_decode("core_events.body", record["body"]) for record in stored]
    assert {event["text"] for event in decoded} == {_BODY}
    assert sum(event["visibility"] == "private" for event in decoded) == 2
    family = [event for event in decoded if event["visibility"] == "group"]
    assert len(family) == 2 and all(event["metadata"]["wechat"]["trigger"] == "none" for event in family)

    source_checkpoint = gateway.inbox.checkpoint(gateway.source_key)
    core_cursor = pg_store.state(collector.cursor_key)
    assert source_checkpoint == advanced and core_cursor["offset"] == 4
    pending = pg_store.state(gateway.inbox.key)
    assert len(pending["pending"]) == 4 and pending["acknowledged"] == 0
    # Core commit does not eagerly acknowledge the just-returned inbox batch.
    with pg_store.db() as db:
        for key in (collector.cursor_key, gateway.inbox._checkpoint_key(gateway.source_key)):
            assert db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()[0].startswith("enc1:")

    with independent_store(pg_store) as restarted_store:
        # Source replay is harmless even after core commit; independent receipts
        # reject re-enqueue and the saved core cursor acknowledges the old batch.
        resumed, repeated_worker, next_collector = flow(restarted_store, [advanced, data])
        result = await next_collector.run(once=True)
        assert repeated_worker.calls[0][1]["enrollment"] == advanced["enrollment"]
        assert repeated_worker.calls[0][1]["checkpoint"] == advanced["checkpoint"]
        assert resumed.inbox.epoch == gateway.inbox.epoch
        assert restarted_store.state(next_collector.cursor_key) == core_cursor
        assert resumed.inbox.checkpoint(resumed.source_key) == source_checkpoint
        assert result["events_committed"] == result["events_replayed"] == 0
        assert {record["id"] for record in core_rows(restarted_store)} == {record["id"] for record in stored}
        saved = restarted_store.state(resumed.inbox.key)
        assert saved["pending"] == [] and saved["acknowledged"] == 4
        assert_receive_only(restarted_store, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_cursor_write", [False, True], ids=["write-rejected", "failure-after-write"])
async def test_pg_core_cursor_failure_rolls_back_events_then_pending_replays_once(
        pg_store, monkeypatch, after_cursor_write):
    initial, data, advanced = four_rows()
    gateway, worker, collector = flow(pg_store, [initial, data])
    original = pg_store.set_state

    def fail_cursor(key, value, db=None):
        if key == collector.cursor_key:
            if after_cursor_write:
                original(key, value, db)
            raise AssistantError("database_error", "synthetic cursor transaction interruption")
        return original(key, value, db)

    with monkeypatch.context() as patch:
        patch.setattr(pg_store, "set_state", fail_cursor)
        with pytest.raises(AssistantError) as caught:
            await collector.run(once=True)
    assert caught.value.code == "database_error" and worker.closed
    assert not collector._lock.is_locked

    with independent_store(pg_store) as restarted_store:
        # Source commit intentionally survives: it owns the original pending
        # observations, so rolling back core never requires a new source baseline.
        assert restarted_store.state(collector.cursor_key) is None
        assert core_rows(restarted_store) == []
        with restarted_store.db() as db:
            assert db.execute("SELECT count(*) FROM actor_nodes").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM governance_policies WHERE resource LIKE 'event/%'").fetchone()[0] == 0
        assert gateway.inbox.checkpoint(gateway.source_key) == advanced
        pending = restarted_store.state(gateway.inbox.key)
        assert len(pending["pending"]) == 4 and pending["acknowledged"] == 0

        empty, _ = batch(advanced)
        resumed, resumed_worker, recovered = flow(restarted_store, [advanced, empty])
        result = await recovered.run(once=True)
        assert resumed_worker.calls[0][1]["checkpoint"] == advanced["checkpoint"]
        assert result["events_committed"] == 4 and result["events_replayed"] == 0
        stored = core_rows(restarted_store)
        assert len(stored) == len({record["id"] for record in stored}) == 4
        assert all(record["body"].startswith("enc1:") for record in stored)
        recovered_cursor = restarted_store.state(recovered.cursor_key)
        assert recovered_cursor["offset"] == 4
        assert resumed.inbox.checkpoint(resumed.source_key) == advanced
        assert_receive_only(restarted_store, result)

    with independent_store(pg_store) as final_store:
        final_gateway, _, final_collector = flow(final_store, [advanced, data])
        result = await final_collector.run(once=True)
        assert result["events_committed"] == result["events_replayed"] == 0
        assert final_store.state(final_collector.cursor_key) == recovered_cursor
        assert len(core_rows(final_store)) == 4
        assert final_store.state(final_gateway.inbox.key)["pending"] == []
        assert_receive_only(final_store, result)
