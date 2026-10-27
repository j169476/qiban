"""Bounded parallelism across conversations, with source order within each one."""
import asyncio

from .errors import AssistantError

EXPECTED = {"reply_session_expired", "reply_quota_exhausted", "reply_not_authorized"}


async def handle_batch(core, incoming, *, concurrency=2):
    groups = {}
    for event in incoming:
        groups.setdefault(event.thread, []).append(event)
    slots = asyncio.Semaphore(concurrency)

    async def conversation(events):
        async with slots:
            for event in events:
                if not await asyncio.to_thread(core.remaining_model_calls):
                    return
                try:
                    await core.handle(event)
                except AssistantError as exc:
                    if exc.code not in EXPECTED:
                        raise

    tasks = [asyncio.create_task(conversation(events)) for events in groups.values()]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
