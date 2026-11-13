"""Stop whole application tasks before changing the model-account boundary."""

import asyncio
import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from companion_core.errors import AssistantError

_current = contextvars.ContextVar("desktop_task", default=None)


@dataclass(eq=False)
class Attempt:
    epoch: int
    loop: object
    task: object = None
    stopped: threading.Event = field(default_factory=threading.Event)
    switched: bool = False


class TaskBarrier:
    def __init__(self):
        self.lock = threading.RLock()
        self.epoch = 0
        self.blocked = False
        self.closed = False
        self.attempts = set()
        self.maintaining = False

    @contextmanager
    def idle_maintenance(self):
        """Reserve an idle interval without cancelling tasks or holding a DB lock."""
        with self.lock:
            allowed = not self.attempts and not self.maintaining and not self.closed
            if allowed:
                self.maintaining = True
        try:
            yield allowed
        finally:
            if allowed:
                with self.lock:
                    self.maintaining = False

    def check(self):
        current = _current.get()
        with self.lock:
            if (
                self.closed
                or self.blocked
                or (current and current[0] is self and current[1].epoch != self.epoch)
            ):
                raise AssistantError("model_account_switching", "账号正在切换，旧任务已停止。")

    async def run(self, factory):
        """Restart a cancelled whole task only after the new account is committed.

        Nested model/poll/dispatch work shares its parent attempt. Platform task
        factories must consult the durable inbox and send ledger on every attempt.
        """
        current = _current.get()
        if current and current[0] is self:
            self.check()
            return await factory()
        while True:
            with self.lock:
                if self.closed:
                    raise AssistantError("runtime_closed", "应用任务已停止。")
                if not self.blocked and not self.maintaining:
                    attempt = Attempt(self.epoch, asyncio.get_running_loop())
                    self.attempts.add(attempt)
                    attempt.task = asyncio.create_task(self._attempt(attempt, factory))
                    break
            await asyncio.sleep(0.02)
        try:
            try:
                result = await asyncio.shield(attempt.task)
            except asyncio.CancelledError:
                # An explicit caller cancellation always wins over a switch restart.
                if asyncio.current_task().cancelling():
                    if not attempt.switched:
                        attempt.task.cancel()
                    # Repeated caller cancellation must not cut short child cleanup
                    # and make drain() acknowledge a still-running worker.
                    while not attempt.task.done():
                        try:
                            await asyncio.shield(attempt.task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    raise
                if not attempt.switched:
                    raise
            except AssistantError as error:
                if not attempt.switched or error.code != "model_account_switching":
                    raise
            else:
                with self.lock:
                    if not attempt.switched and attempt.epoch == self.epoch:
                        return result
        finally:
            with self.lock:
                self.attempts.discard(attempt)
                attempt.stopped.set()
        return await self.run(factory)

    async def _attempt(self, attempt, factory):
        token = _current.set((self, attempt))
        try:
            self.check()
            return await factory()
        finally:
            _current.reset(token)

    def block(self):
        """Close admission and signal every old task without waiting on database I/O."""
        with self.lock:
            self.blocked = True
            self.epoch += 1
            attempts = list(self.attempts)
            for attempt in attempts:
                if attempt.switched:
                    continue
                attempt.switched = True
                if not attempt.task.done():
                    attempt.loop.call_soon_threadsafe(attempt.task.cancel)
            return attempts

    def drain(self, attempts, timeout=10):
        deadline = time.monotonic() + timeout
        for attempt in attempts:
            if not attempt.stopped.wait(max(0, deadline - time.monotonic())):
                raise AssistantError(
                    "model_tasks_stopping", "旧任务尚未全部退出，账号未切换；请稍后重新选择账号。"
                )

    def release(self):
        with self.lock:
            if not self.closed:
                self.blocked = False

    def close(self):
        with self.lock:
            self.closed = True
        self.drain(self.block())
