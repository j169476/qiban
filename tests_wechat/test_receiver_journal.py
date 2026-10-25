"""Synthetic journal tests; use the existing state table and production codec.

SQLite supplies isolated test transactions; PostgreSQL's actual ledger codec
encrypts every value. No production database, account, or credential is opened.
"""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from companion_core.cipher import ContentCipher
from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore
from companion_core.storage import Store
from companion_wechat.contracts import Observation
from companion_wechat.durable_inbox import DurableInbox

_ACCOUNT = "synthetic-journal-account"


class EncryptedTestStore(Store):
    ledger_encode = PostgresStore.ledger_encode
    ledger_decode = PostgresStore.ledger_decode

    def __init__(self, root):
        self.cipher = ContentCipher(bytes(range(32)))
        super().__init__(root)


@pytest.fixture
def inbox(tmp_path):
    return DurableInbox(EncryptedTestStore(tmp_path / "journal"), _ACCOUNT)


def observation(inbox, number=1, *, text="synthetic message"):
    return Observation(f"{number:032x}", inbox.account, inbox.epoch, "synthetic-target", "private",
                       "2026-10-24T00:00:00+00:00", text, trigger="direct")


def publish(inbox, observations=(), *, expected=None, checkpoint=None, source="synthetic-source"):
    return inbox.publish_batch(observations, checkpoint_key=source,
                               expected_checkpoint=expected, checkpoint=checkpoint or {"row": 0})


@pytest.mark.asyncio
async def test_stable_epoch_checkpoint_and_unacked_payload_survive_restart(inbox):
    original = observation(inbox)
    assert publish(inbox, [original], checkpoint={"row": 1}) == 1
    batch = await inbox.poll({})
    restarted = DurableInbox(EncryptedTestStore(inbox.store.root), _ACCOUNT)
    assert restarted.epoch == inbox.epoch
    assert restarted.key == inbox.key
    assert restarted.checkpoint("synthetic-source") == {"row": 1}
    assert await restarted.poll({}) == batch
    await restarted.acknowledge(batch.cursor)
    again = DurableInbox(EncryptedTestStore(inbox.store.root), _ACCOUNT)
    assert not again.publish(original)
    assert not (await again.poll(batch.cursor)).observations
    with pytest.raises(AssistantError) as caught:
        again.publish(replace(original, text="changed after acknowledgement"))
    assert caught.value.code == "wechat_observation_changed"


def test_account_and_explicit_epoch_namespaces_are_separate(inbox):
    other = DurableInbox(inbox.store, "different-synthetic-account")
    explicit = DurableInbox(inbox.store, _ACCOUNT, "fake-explicit-epoch")
    assert len({inbox.epoch, other.epoch, explicit.epoch}) == 3
    assert len({inbox.key, other.key, explicit.key}) == 3
    publish(inbox, checkpoint={"row": 1})
    assert other.checkpoint("synthetic-source") is None
    assert explicit.checkpoint("synthetic-source") is None
    assert DurableInbox(inbox.store, _ACCOUNT).epoch == inbox.epoch


@pytest.mark.asyncio
async def test_batch_duplicates_dedupe_and_conflict_rolls_back_entire_batch(inbox):
    first, second = observation(inbox), observation(inbox, 2)
    assert publish(inbox, [first, first], checkpoint={"row": 1}) == 1
    with pytest.raises(AssistantError) as caught:
        publish(inbox, [second, replace(first, text="changed")], expected={"row": 1}, checkpoint={"row": 2})
    assert caught.value.code == "wechat_observation_changed"
    assert inbox.checkpoint("synthetic-source") == {"row": 1}
    assert (await inbox.poll({})).observations == (first,)
    assert inbox.store.state(inbox._receipt_key(second.observation_id)) is None
    with pytest.raises(AssistantError) as caught:
        publish(inbox, [second, replace(second, sender_ref="changed")],
                expected={"row": 1}, checkpoint={"row": 2})
    assert caught.value.code == "wechat_observation_changed"


@pytest.mark.asyncio
async def test_capacity_error_does_not_advance_checkpoint_or_keep_receipt(inbox):
    queue = DurableInbox(inbox.store, _ACCOUNT, capacity=1)
    first, second = observation(queue), observation(queue, 2)
    publish(queue, [first], checkpoint={"row": 1})
    with pytest.raises(AssistantError) as caught:
        publish(queue, [second], expected={"row": 1}, checkpoint={"row": 2})
    assert caught.value.code == "wechat_inbox_full"
    assert queue.checkpoint("synthetic-source") == {"row": 1}
    assert queue.store.state(queue._receipt_key(second.observation_id)) is None
    assert (await queue.poll({})).observations == (first,)
    # Re-observation adds no pending item and may checkpoint filtered input.
    assert publish(queue, [first], expected={"row": 1}, checkpoint={"row": 2}) == 0


@pytest.mark.asyncio
async def test_checkpoint_cas_conflict_is_type_sensitive_and_has_no_side_effect(inbox):
    publish(inbox, checkpoint={"row": 1})
    for stale in (None, {"row": 0}, {"row": True}, {"row": 1.0}):
        with pytest.raises(AssistantError) as caught:
            publish(inbox, [observation(inbox)], expected=stale, checkpoint={"row": 2})
        assert caught.value.code == "wechat_checkpoint_conflict"
    assert not (await inbox.poll({})).observations
    assert inbox.checkpoint("synthetic-source") == {"row": 1}


def test_checkpoint_return_and_input_are_detached_from_caller_mutation(inbox):
    checkpoint = {"source": {"row": 1}}
    publish(inbox, checkpoint=checkpoint)
    checkpoint["source"]["row"] = 2
    loaded = inbox.checkpoint("synthetic-source")
    loaded["source"]["row"] = 3
    assert inbox.checkpoint("synthetic-source") == {"source": {"row": 1}}


@pytest.mark.parametrize("checkpoint", [None, [], "secret", {"row": float("nan")}, {"large": "x" * 65536}])
def test_invalid_checkpoint_has_no_state_mutation(inbox, checkpoint):
    with pytest.raises(AssistantError) as caught:
        inbox.publish_batch([observation(inbox)], checkpoint_key="source", expected_checkpoint=None,
                            checkpoint=checkpoint)
    assert caught.value.code == "wechat_checkpoint_invalid"
    assert inbox.checkpoint("source") is None
    assert inbox.store.state(inbox._receipt_key(observation(inbox).observation_id)) is None


def test_only_one_of_two_concurrent_checkpoint_writers_can_commit(inbox):
    publish(inbox, checkpoint={"row": 0})
    barrier = Barrier(2)

    def contender(number):
        queue = DurableInbox(EncryptedTestStore(inbox.store.root), _ACCOUNT)
        barrier.wait(timeout=10)
        try:
            publish(queue, [observation(queue, number)], expected={"row": 0}, checkpoint={"row": number})
            return "committed"
        except AssistantError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, (1, 2)))
    assert sorted(results) == ["committed", "wechat_checkpoint_conflict"]
    assert len(inbox.store.state(inbox.key)["pending"]) == 1


@pytest.mark.asyncio
async def test_ack_cannot_skip_unoffered_rows_regress_or_change_stream(inbox):
    publish(inbox, [observation(inbox), observation(inbox, 2)], checkpoint={"row": 2})
    first = await inbox.poll({}, limit=1)
    for cursor in ({**first.cursor, "offset": 2}, {**first.cursor, "offset": True},
                   {**first.cursor, "wechat_stream": "other"}, [first.cursor]):
        with pytest.raises(AssistantError):
            await inbox.acknowledge(cursor)
    await inbox.acknowledge(first.cursor)
    await inbox.acknowledge(first.cursor)  # Idempotent acknowledgments are valid.
    for cursor in ({}, {**first.cursor, "offset": 0}):
        with pytest.raises(AssistantError):
            await inbox.acknowledge(cursor)
    second = await inbox.poll(first.cursor)
    assert second.observations == (observation(inbox, 2),)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after", [1, 2, 3, 4])
async def test_process_crash_rolls_back_receipts_pending_and_checkpoint(inbox, crash_after):
    # Exit after writing receipt 1, receipt 2, queue, or checkpoint but before
    # transaction commit. Recovery must find either all committed data or none.
    script = r'''
import os,sys
from pathlib import Path
from companion_core.cipher import ContentCipher
from companion_core.postgres import PostgresStore
from companion_core.storage import Store
from companion_wechat.contracts import Observation
from companion_wechat.durable_inbox import DurableInbox
class TestStore(Store):
    ledger_encode=PostgresStore.ledger_encode
    ledger_decode=PostgresStore.ledger_decode
    def __init__(self,root):
        self.cipher=ContentCipher(bytes(range(32)))
        super().__init__(root)
s=TestStore(Path(sys.argv[1]))
q=DurableInbox(s,'synthetic-journal-account')
original=s.set_state
calls=0
def interrupted(key,value,db=None):
    global calls
    original(key,value,db)
    calls+=1
    if calls==int(sys.argv[2]):
        os._exit(78)
s.set_state=interrupted
rows=[Observation(f'{i:032x}',q.account,q.epoch,'synthetic-target','private',
                  '2026-10-24T00:00:00+00:00','synthetic message',trigger='direct') for i in (1,2)]
q.publish_batch(rows,checkpoint_key='synthetic-source',expected_checkpoint=None,checkpoint={'row':2})
'''
    child = subprocess.run([sys.executable, "-c", script, str(inbox.store.root), str(crash_after)],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 78, child.stderr
    restarted = DurableInbox(EncryptedTestStore(inbox.store.root), _ACCOUNT)
    assert restarted.epoch == inbox.epoch
    assert restarted.checkpoint("synthetic-source") is None
    assert not (await restarted.poll({})).observations
    assert all(restarted.store.state(restarted._receipt_key(observation(restarted, i).observation_id)) is None
               for i in (1, 2))
    assert publish(restarted, [observation(restarted), observation(restarted, 2)], checkpoint={"row": 2}) == 2


@pytest.mark.asyncio
async def test_legacy_seen_migrates_without_losing_ack_or_pending(inbox):
    first, second = observation(inbox), observation(inbox, 2)
    publish(inbox, [first, second], checkpoint={"row": 2})
    batch = await inbox.poll({}, limit=1)
    await inbox.acknowledge(batch.cursor)
    old = inbox.store.state(inbox.key)
    old.pop("version")
    old["seen"] = {first.observation_id: first.fingerprint, second.observation_id: second.fingerprint}
    with inbox.store.lock, inbox.store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        inbox.store.set_state(inbox.key, old, db)
        for item in (first, second):
            db.execute("DELETE FROM state WHERE key=?", (inbox._receipt_key(item.observation_id),))
    migrated = DurableInbox(EncryptedTestStore(inbox.store.root), _ACCOUNT)
    assert "seen" not in migrated.store.state(migrated.key)
    assert not migrated.publish(first)
    remaining = await migrated.poll(batch.cursor)
    assert remaining.observations == (second,)
    assert remaining.cursor["wechat_stream"] == batch.cursor["wechat_stream"]
    assert migrated.checkpoint("synthetic-source") == {"row": 2}


@pytest.mark.asyncio
async def test_more_than_ten_thousand_receipts_do_not_fill_pending_queue(inbox):
    queue = DurableInbox(inbox.store, _ACCOUNT, capacity=500)
    cursor, expected = {}, None
    total = 10051
    for start in range(1, total + 1, 500):
        end = min(total, start + 499)
        rows = [observation(queue, number) for number in range(start, end + 1)]
        checkpoint = {"row": end}
        assert publish(queue, rows, expected=expected, checkpoint=checkpoint) == len(rows)
        expected = checkpoint
        remaining = len(rows)
        while remaining:
            batch = await queue.poll(cursor)
            assert batch.observations
            remaining -= len(batch.observations)
            cursor = batch.cursor
            await queue.acknowledge(cursor)
    state = queue.store.state(queue.key)
    assert state["sequence"] == state["acknowledged"] == total
    assert state["pending"] == [] and "seen" not in state
    assert not queue.publish(observation(queue, 1))
    with queue.store.db() as db:
        count = db.execute("SELECT count(*) FROM state WHERE key LIKE ?",
                           (queue.key + ":receipt:%",)).fetchone()[0]
    assert count == total


def test_receipts_checkpoints_and_pending_use_existing_encrypted_state(inbox):
    original = observation(inbox, text="synthetic private payload")
    publish(inbox, [original], checkpoint={"private-source-marker": 1})
    with inbox.store.db() as db:
        records = db.execute("SELECT key,value FROM state").fetchall()
    assert all(value.startswith("enc1:") for _, value in records)
    assert all("synthetic private payload" not in value and "private-source-marker" not in value
               for _, value in records)
    receipt = inbox.store.state(inbox._receipt_key(original.observation_id))
    assert receipt == original.fingerprint  # No second permanent copy of message content.
