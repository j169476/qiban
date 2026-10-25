from dataclasses import replace

import pytest

from companion_core.errors import AssistantError
from companion_core.storage import Store
from companion_wechat.contracts import Observation
from companion_wechat.durable_inbox import DurableInbox


def observation(identifier="1" * 32):
    return Observation(identifier, "synthetic-account", "epoch", "target", "private",
                       "2026-10-24T00:00:00+00:00", "same text", trigger="direct")


@pytest.mark.asyncio
async def test_restart_replays_uncommitted_batch_but_not_acknowledged_batch(tmp_path):
    store = Store(tmp_path / "queue")
    queue = DurableInbox(store, "synthetic-account", "epoch")
    first, second = observation(), observation("2" * 32)
    assert queue.publish(first) and queue.publish(second)
    batch = await queue.poll({})
    assert len(batch.observations) == 2  # Equal text is two distinct messages.
    restarted = DurableInbox(Store(store.root), "synthetic-account", "epoch")
    assert await restarted.poll({}) == batch
    await restarted.acknowledge(batch.cursor)
    again = DurableInbox(Store(store.root), "synthetic-account", "epoch")
    assert not (await again.poll(batch.cursor)).observations
    assert not again.publish(first)  # Acknowledgment does not erase dedupe receipts.
    with pytest.raises(AssistantError):
        await again.poll({})


@pytest.mark.asyncio
async def test_conflicting_observation_scope_cursor_and_overflow_fail_closed(tmp_path):
    queue = DurableInbox(Store(tmp_path / "queue"), "synthetic-account", "epoch", capacity=1)
    original = observation()
    queue.publish(original)
    for invalid in (replace(original, text="changed"), replace(original, account_identity="other"),
                    observation("2" * 32)):
        with pytest.raises(AssistantError):
            queue.publish(invalid)
    batch = await queue.poll({})
    assert batch.observations == (original,)
    with pytest.raises(AssistantError):
        await queue.acknowledge({**batch.cursor, "offset": 100})
    with pytest.raises(AssistantError):
        await queue.acknowledge({**batch.cursor, "wechat_stream": "other"})


@pytest.mark.asyncio
async def test_gateway_recreation_uses_durable_receipt_and_cursor(tmp_path):
    from companion_wechat.gateway import PermitAuthority
    from companion_wechat.providers.fake import FakeGateway

    store = Store(tmp_path / "gateway")
    authority = PermitAuthority(b"synthetic-restart-key" * 3)
    first = FakeGateway("synthetic-account", authority, epoch="epoch", inbox_store=store)
    first.publish(observation())
    batch = await first.poll({}, limit=10)
    second = FakeGateway("synthetic-account", authority, epoch="epoch", inbox_store=Store(store.root))
    assert await second.poll({}, limit=10) == batch
    await second.acknowledge(batch.cursor)
    assert not second.publish(observation())
    assert not (await second.poll(batch.cursor, limit=10)).observations
