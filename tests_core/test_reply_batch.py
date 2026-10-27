import asyncio
from types import SimpleNamespace

import pytest

from companion_core.reply_batch import handle_batch


@pytest.mark.asyncio
async def test_conversations_overlap_but_each_keeps_source_order_and_bound():
    first_started, release = asyncio.Event(), asyncio.Event()
    log, active, peak = [], 0, 0

    async def handle(event):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        log.append((event.id, "start"))
        if event.id == "a1":
            first_started.set()
            await release.wait()
        if event.id == "b1":
            await first_started.wait()
            assert ("a2", "start") not in log
            release.set()
        log.append((event.id, "end"))
        active -= 1

    core = SimpleNamespace(handle=handle, remaining_model_calls=lambda: 10)
    incoming = [SimpleNamespace(id=key, thread=key[0]) for key in ["a1", "a2", "b1", "c1"]]
    await asyncio.wait_for(handle_batch(core, incoming), 3)
    assert peak == 2
    assert log.index(("a1", "end")) < log.index(("a2", "start"))


@pytest.mark.asyncio
async def test_failure_waits_for_other_conversation_to_settle():
    completed = []

    async def handle(event):
        if event.thread == "bad":
            raise RuntimeError("synthetic")
        await asyncio.sleep(.01)
        completed.append(event.thread)

    core = SimpleNamespace(handle=handle, remaining_model_calls=lambda: 10)
    with pytest.raises(RuntimeError):
        await handle_batch(core, [SimpleNamespace(thread="bad"), SimpleNamespace(thread="good")])
    assert completed == ["good"]


def test_only_complete_acknowledgments_skip_query_expansion():
    from companion_core.recall_routing import acknowledgment

    assert acknowledgment("谢谢你！") and acknowledgment("Hello!")
    for text in ["你好，还记得我的住址吗", "thanks, what was my old name", "好的？", "早上好，帮我查一下"]:
        assert not acknowledgment(text)
