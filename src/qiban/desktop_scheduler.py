"""Application-owned platform loops, with no chat or browser lifecycle dependency."""

import asyncio
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

from companion_core.delivery import TEMPORARY
from companion_core.errors import AssistantError
from companion_core.reply_batch import handle_batch
from companion_core.runtime_keys import DESKTOP_PLATFORMS as PLATFORMS


@dataclass
class Schedule:
    generation: str | None = None
    next_poll: float = 0
    next_cycle: float = 0
    failures: int = 0


class DesktopScheduler:
    def __init__(self, runtime, *, clock=time.monotonic, tick=0.25, maintenance=None):
        self.runtime, self.clock, self.tick = runtime, clock, tick
        self.lock = threading.RLock()
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.loop = None
        self.jobs = {}
        self.cancelling = set()
        self.busy = set()
        self.failure = None
        self.maintenance, self.maintenance_error = maintenance, None
        self.schedules = {platform: Schedule() for platform in PLATFORMS}
        self.platform_locks = {platform: asyncio.Lock() for platform in PLATFORMS}

    def open(self):
        with self.lock:
            if self.thread is not None:
                raise AssistantError("scheduler_started", "后台任务调度已启动。")
            self.thread = threading.Thread(target=self._thread, name="qiban-platforms", daemon=True)
            self.thread.start()
        if not self.ready.wait(5) or self.failure:
            raise AssistantError("scheduler_failed", "后台任务调度启动失败。")

    def _thread(self):
        try:
            asyncio.run(self._run())
        except BaseException:
            self.failure = "scheduler_failed"
            self.runtime.tasks.block()
        finally:
            self.ready.set()

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.ready.set()
        workers = [asyncio.create_task(self._work(platform)) for platform in PLATFORMS]
        if getattr(self.runtime, 'manual', None) is not None:
            workers.append(asyncio.create_task(self._manual_work()))
        maintenance = asyncio.create_task(self._maintain()) if self.maintenance is not None else None
        try:
            while not self.stop_requested.is_set():
                for worker in workers:
                    if worker.done():
                        worker.result()
                        raise AssistantError("scheduler_failed", "平台调度意外退出。")
                await asyncio.sleep(self.tick)
        finally:
            if maintenance is not None:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            async with AsyncExitStack() as cleanup:
                for core in self.runtime.cores.values():
                    for adapter in core.platforms.all():
                        close = getattr(adapter, "close", None)
                        if close:
                            cleanup.push_async_callback(close)

    async def _maintain(self):
        while not self.stop_requested.is_set():
            operation = asyncio.create_task(asyncio.to_thread(self.maintenance))
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                # A database transaction must finish/roll back before shutdown.
                await asyncio.gather(operation, return_exceptions=True)
                raise
            except Exception as error:
                self.maintenance_error = error.code if isinstance(error, AssistantError) else 'media_cleanup_failed'
                return  # Visible failure; no silent repeated deletion attempts.
            await asyncio.sleep(60)

    async def _manual_work(self):
        while not self.stop_requested.is_set():
            try:
                await self.runtime.tasks.run(self.runtime.manual.step)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # A manual cancel only ends this attempt, not the task worker.
            except AssistantError as error:
                if error.code == 'runtime_closed' and self.stop_requested.is_set():
                    return
                raise
            await asyncio.sleep(self.tick)

    def status(self, platform):
        with self.lock:
            return {
                "busy": platform in self.busy,
                "stopping": platform in self.cancelling,
                "scheduler_error": self.failure,
            }

    def cancel(self, platform):
        with self.lock:
            job = self.jobs.get(platform)
            if job is not None and not job.done() and platform not in self.cancelling:
                self.cancelling.add(platform)
                self.loop.call_soon_threadsafe(job.cancel)

    def wake(self, platform):
        def reset():
            if platform in self.schedules:
                self.schedules[platform].next_poll = 0

        if self.loop is not None:
            self.loop.call_soon_threadsafe(reset)

    def close(self):
        self.stop_requested.set()
        for platform in self.runtime.cores:
            self.cancel(platform)
        if self.thread is not None:
            self.thread.join(15)
            if self.thread.is_alive():
                raise AssistantError("scheduler_stopping", "后台任务仍在退出，请稍候。")

    def replace_platform(self, platform, commit):
        async def replace():
            async with self.platform_locks[platform]:
                core = self.runtime.cores.get(platform)
                if core is not None:
                    for adapter in core.platforms.all():
                        close = getattr(adapter, "close", None)
                        if close:
                            await close()
                commit()
                self.schedules[platform] = Schedule()

        if self.failure or self.stop_requested.is_set() or self.loop is None:
            raise AssistantError("scheduler_failed", "后台任务调度尚未就绪。")
        future = asyncio.run_coroutine_threadsafe(replace(), self.loop)
        try:
            future.result(timeout=15)
        except Exception:
            future.cancel()
            raise AssistantError("platform_reconfigure_pending", "设置已保存，但连接器尚未更新完成；请重启应用后台。") from None

    async def _work(self, platform):
        while not self.stop_requested.is_set():
            state = self.schedules[platform]
            if self.runtime.tasks.blocked or self.clock() < state.next_poll:
                await asyncio.sleep(self.tick)
                continue
            with self.lock:
                job = asyncio.create_task(self.runtime.tasks.run(lambda: self._step(platform)))
                self.jobs[platform] = job
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    self.cancel(platform)
                    await asyncio.gather(job, return_exceptions=True)
                    raise
                # Explicit platform stop cancelled this job; the loop remains
                # available for a later explicit start, after cleanup is done.
            except (AssistantError, TimeoutError) as error:
                code = error.code if isinstance(error, AssistantError) else "inbox_timeout"
                state.failures += 1
                state.next_poll = self.clock() + min(900, 60 * 2 ** min(state.failures, 4))
                if code not in TEMPORARY | {"cycle_busy"} and (
                    state.failures >= 3 or code not in {"inbox_timeout", "network_error"}
                ):
                    await asyncio.to_thread(self.runtime.fail, platform, code, state.generation)
            except Exception:
                await asyncio.to_thread(self.runtime.fail, platform, "runtime_failed", state.generation)
            finally:
                with self.lock:
                    self.jobs.pop(platform, None)
                    self.cancelling.discard(platform)
                    self.busy.discard(platform)
            await asyncio.sleep(self.tick)

    async def _step(self, platform):
        async with self.platform_locks[platform]:
            await self._step_locked(platform)

    async def _step_locked(self, platform):
        core = self.runtime.cores.get(platform)
        if core is None:
            return
        state = self.schedules[platform]
        run = await asyncio.to_thread(self.runtime.store.state, core.run_key, {})
        if run.get("status") != "active":
            for adapter in core.platforms.all():
                idle = getattr(adapter, "idle", None)
                if idle:
                    await idle()
            return
        with self.lock:
            self.busy.add(platform)
        run = await asyncio.to_thread(core.guard)
        if run["generation"] != state.generation:
            state.generation, state.next_poll, state.next_cycle, state.failures = run["generation"], 0, 0, 0
        settings = (await asyncio.to_thread(self.runtime.store.read, "companion"))["data"]
        adapters = core.platforms.all()
        custom = bool(adapters) and all(hasattr(adapter, "runtime_poll") for adapter in adapters)
        if custom:
            incoming = []
            for adapter in adapters:
                incoming.extend(
                    await asyncio.wait_for(adapter.runtime_poll(), settings.get("inbox_timeout_seconds", 180))
                )
        else:
            incoming = await asyncio.wait_for(core.poll(), settings.get("inbox_timeout_seconds", 180))
            incoming = [event for event in incoming if event.visibility == "private"]
        state.failures = 0
        state.next_poll = self.clock() + min(
            (getattr(a, "poll_interval_seconds", settings.get("inbox_poll_seconds", 60)) for a in adapters),
            default=60,
        )
        await handle_batch(
            core, incoming[: settings.get("inbox_batch_size", 3)], concurrency=2 if custom else 1
        )
        await asyncio.to_thread(core.guard, run["generation"])
        if not custom and not run.get("reply_test") and self.clock() >= state.next_cycle:
            await core.cycle()
            policy = (await asyncio.to_thread(self.runtime.store.read, "policy"))["data"]
            state.next_cycle = self.clock() + policy.get("cycle_minutes", 15) * 60
