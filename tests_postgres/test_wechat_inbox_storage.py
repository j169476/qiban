import pytest

from companion_wechat.contracts import Observation
from companion_wechat.durable_inbox import DurableInbox


@pytest.mark.asyncio
async def test_wechat_pending_observations_are_encrypted_and_recover_after_recreation(pg_store):
    queue = DurableInbox(pg_store, "synthetic-self", "epoch")
    value = Observation("1" * 32, "synthetic-self", "epoch", "target", "private",
                        "2026-10-24T00:00:00+00:00", "private-inbox-sentinel", trigger="direct")
    assert queue.publish(value)
    with pg_store.db() as db:
        rows = db.execute("SELECT value FROM state WHERE key LIKE ?", ("wechat-inbox:%",)).fetchall()
        assert rows and all(row[0].startswith("enc1:") and "private-inbox-sentinel" not in row[0] for row in rows)
    batch = await queue.poll({})
    recovered = DurableInbox(pg_store, "synthetic-self", "epoch")
    assert (await recovered.poll({})).observations == (value,)
    await recovered.acknowledge(batch.cursor)
    assert not (await DurableInbox(pg_store, "synthetic-self", "epoch").poll(batch.cursor)).observations
