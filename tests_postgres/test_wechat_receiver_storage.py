"""Durable receive journal on the fixture's fresh synthetic PostgreSQL schema."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
from dataclasses import replace
from threading import Barrier, RLock

import pytest

from companion_core.errors import AssistantError
from companion_core.pg_connection import Database
from companion_wechat.contracts import Observation
from companion_wechat.durable_inbox import DurableInbox

_ACCOUNT = "synthetic-pg-receiver"
_SOURCE = "synthetic-source"


def observation(inbox, number=1):
    return Observation(f"{number:032x}", inbox.account, inbox.epoch, "synthetic-target", "private",
                       "2026-10-24T00:00:00+00:00", "private-receiver-sentinel", trigger="direct")


def publish(inbox, observations, *, expected=None, checkpoint=None):
    return inbox.publish_batch(observations, checkpoint_key=_SOURCE, expected_checkpoint=expected,
                               checkpoint=checkpoint or {"row": 2, "source": "private-checkpoint-sentinel"})


@contextmanager
def independent_store(pg_store):
    # A fresh connection pool and independent local lock, with the same host's
    # in-memory encryption provider. Never export fixture credentials or keys.
    other = copy(pg_store)
    other.database = Database(pg_store.database._options)
    other.lock = RLock()
    try:
        yield other
    finally:
        other.database.close()


@pytest.mark.asyncio
async def test_pg_restart_replays_unacked_batch_and_all_journal_values_are_encrypted(pg_store):
    inbox = DurableInbox(pg_store, _ACCOUNT)
    value = observation(inbox)
    assert publish(inbox, [value]) == 1
    batch = await inbox.poll({})
    with pg_store.db() as db:
        values = [row[0] for row in db.execute(
            "SELECT value FROM state WHERE key LIKE ? OR key LIKE ?",
            ("wechat-inbox:%", "wechat-ingestion:%"))]
    assert len(values) == 4  # Stable epoch, pending queue, receipt, checkpoint.
    assert all(value.startswith("enc1:") for value in values)
    assert all("private-receiver-sentinel" not in value and "private-checkpoint-sentinel" not in value
               for value in values)
    with independent_store(pg_store) as restarted_store:
        restarted = DurableInbox(restarted_store, _ACCOUNT)
        assert restarted.epoch == inbox.epoch
        assert restarted.checkpoint(_SOURCE) == {"row": 2, "source": "private-checkpoint-sentinel"}
        assert await restarted.poll({}) == batch
        await restarted.acknowledge(batch.cursor)
    with independent_store(pg_store) as next_store:
        after_ack = DurableInbox(next_store, _ACCOUNT)
        assert not (await after_ack.poll(batch.cursor)).observations
        assert not after_ack.publish(value)
        assert next_store.state(after_ack._receipt_key(value.observation_id)) == value.fingerprint
        with pytest.raises(AssistantError) as caught:
            after_ack.publish(replace(value, text="different synthetic content"))
        assert caught.value.code == "wechat_observation_changed"
        with pytest.raises(AssistantError):
            await after_ack.poll({})


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after", [1, 2, 3, 4])
async def test_pg_failure_after_each_write_rolls_back_entire_batch(pg_store, monkeypatch, fail_after):
    inbox = DurableInbox(pg_store, _ACCOUNT)
    rows = [observation(inbox, 1), observation(inbox, 2)]
    original = pg_store.set_state
    calls = 0

    class InterruptedWriter(RuntimeError):
        pass

    def interrupted(key, value, db=None):
        nonlocal calls
        original(key, value, db)
        calls += 1
        if calls == fail_after:
            raise InterruptedWriter("synthetic transaction interruption")

    with monkeypatch.context() as patch:
        patch.setattr(pg_store, "set_state", interrupted)
        with pytest.raises(InterruptedWriter):
            publish(inbox, rows)
    # Use a different server session: this must inspect committed PostgreSQL
    # state, not transaction-local objects left in the interrupted writer.
    with independent_store(pg_store) as fresh_store:
        recovered = DurableInbox(fresh_store, _ACCOUNT)
        assert recovered.epoch == inbox.epoch
        assert recovered.checkpoint(_SOURCE) is None
        assert not (await recovered.poll({})).observations
        assert all(fresh_store.state(recovered._receipt_key(row.observation_id)) is None for row in rows)
        assert publish(recovered, rows) == 2
        assert (await recovered.poll({})).observations == tuple(rows)


@pytest.mark.asyncio
async def test_pg_capacity_and_payload_conflict_do_not_advance_checkpoint(pg_store):
    inbox = DurableInbox(pg_store, _ACCOUNT, capacity=1)
    first, second = observation(inbox), observation(inbox, 2)
    assert publish(inbox, [first], checkpoint={"row": 1}) == 1
    for rows, code in (([second], "wechat_inbox_full"),
                       ([replace(first, text="changed")], "wechat_observation_changed")):
        with pytest.raises(AssistantError) as caught:
            publish(inbox, rows, expected={"row": 1}, checkpoint={"row": 2})
        assert caught.value.code == code
    with independent_store(pg_store) as fresh_store:
        recovered = DurableInbox(fresh_store, _ACCOUNT, capacity=1)
        assert recovered.checkpoint(_SOURCE) == {"row": 1}
        assert fresh_store.state(recovered._receipt_key(second.observation_id)) is None
        assert (await recovered.poll({})).observations == (first,)


@pytest.mark.asyncio
async def test_pg_checkpoint_cas_uses_database_lock_across_independent_pools(pg_store):
    inbox = DurableInbox(pg_store, _ACCOUNT)
    assert publish(inbox, [], checkpoint={"row": 0}) == 0
    barrier = Barrier(2)

    def contender(number):
        with independent_store(pg_store) as store:
            writer = DurableInbox(store, _ACCOUNT)
            barrier.wait(timeout=10)
            try:
                publish(writer, [observation(writer, number)], expected={"row": 0},
                        checkpoint={"row": number})
                return number, "committed"
            except AssistantError as exc:
                return number, exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, (1, 2)))
    assert sorted(code for _, code in results) == ["committed", "wechat_checkpoint_conflict"]
    winner = next(number for number, code in results if code == "committed")
    assert inbox.checkpoint(_SOURCE) == {"row": winner}
    assert (await inbox.poll({})).observations == (observation(inbox, winner),)
