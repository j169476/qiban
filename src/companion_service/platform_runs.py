"""Explicit per-platform lifecycle. Uninstalled connectors cannot start another slot."""
import asyncio
import time
from datetime import timedelta

from companion_core.errors import AssistantError
from companion_core.runtime import Companion
from companion_core.runtime_keys import PLATFORMS


class BoundHost:
    def __init__(self, host):
        self.parent, self.thread_id = host, None

    def identity(self):
        return self.thread_id

    def require_active(self, thread_id=None):
        return self.parent.require_active(thread_id or self.thread_id)

    def __getattr__(self, name):
        return getattr(self.parent, name)


class PlatformRuns:
    def __init__(self, service, model, adapters, *, models=None):
        self.service, self.store = service, service.store
        self.cores, self.jobs, self.busy = {}, [], set()
        for platform in PLATFORMS:
            selected = [adapter for adapter in adapters if adapter.platform == platform]
            if selected:
                self.cores[platform] = Companion(self.store, (models or {}).get(platform, model), BoundHost(service.host), selected,
                                                 run_key="core-run:" + platform)
        if any(adapter.platform not in PLATFORMS for adapter in adapters):
            raise AssistantError("platform_unknown", "平台连接器必须登记为 QQ、微信或 B站")

    def status(self, platform):
        run = self.store.state("core-run:" + platform, {})
        waiting_hours = run.get("status") == "active" and not self.service.platform_hours()
        result = {"platform": platform, "available": platform in self.cores,
                "status": run.get("status", "paused"), "reason": run.get(
                    "reason", "running" if run.get("status") == "active" else
                    "explicit_start_required" if platform in self.cores else "platform_unavailable"),
                "owner": run.get("owner"), "generation": run.get("generation"),
                "read_only": run.get("read_only", False), "deadline_at": run.get("deadline_at"),
                "busy": platform in self.busy}
        if waiting_hours:
            result["reason"] = "outside_active_hours"
        result["waiting_for_schedule"] = waiting_hours
        core = self.cores.get(platform)
        if core:
            details = [adapter.runtime_status() for adapter in core.platforms.all()
                       if hasattr(adapter, "runtime_status")]
            if details:
                result["connectors"] = details
        return result

    def control(self, thread, platform, action, arguments):
        if platform not in PLATFORMS:
            raise AssistantError("platform_unknown", "未知平台")
        if action == "status":
            if arguments:
                raise AssistantError("invalid_control", "状态查询不接受参数")
            return self.status(platform)
        core = self.cores.get(platform)
        if action in {"stop", "pause"}:
            if arguments:
                raise AssistantError("invalid_control", "停止不接受参数")
            if thread not in (self.store.state("service-admin-thread"), self.status(platform)["owner"]):
                raise AssistantError("not_owner", "只有本平台归属聊天或管理聊天可以停止")
            if core:
                core.stop("explicit_stop")
            return self.status(platform)
        if action != "start":
            raise AssistantError("unknown_tool", "未知平台操作")
        if not core:
            raise AssistantError("platform_unavailable", "该平台尚未装配可运行连接器，其他平台保持原状态")
        if set(arguments) - {"read_only", "duration_minutes"} or (
                "read_only" in arguments and type(arguments["read_only"]) is not bool):
            raise AssistantError("invalid_control", "平台启动只接受只读模式和运行时长")
        duration = arguments.get("duration_minutes")
        if duration is not None and (type(duration) is not int or not 1 <= duration <= 1440):
            raise AssistantError("invalid_duration", "运行时长须为 1 至 1440 分钟")
        service = self.service
        if service.maintenance_until > time.time():
            raise AssistantError("maintenance", "正在准备数据恢复")
        if not self.store.state("web-owner"):
            raise AssistantError("setup_required", "先完成本机网页管理员初始化")
        if service.supervisor and not service.supervisor.status()["healthy"]:
            raise AssistantError("supervisor_lost", "独立监护尚未就绪")
        old = self.store.state(core.run_key, {})
        if old.get("status") == "active":
            if old["owner"] != thread or old["read_only"] != arguments.get("read_only", False):
                raise AssistantError("already_running", "先停止本平台，再改变归属或运行模式")
            return {"platform": platform, "run": old, "already_running": True}
        if platform in self.busy:
            raise AssistantError("worker_busy", "本平台上次任务正在结束")
        with self.store.db() as db:
            row = db.execute("SELECT mode FROM sessions WHERE thread=?", (thread,)).fetchone()
            if row and row["mode"] != "role":
                raise AssistantError("mode_conflict", "管理聊天不能接管平台运行")
            db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT DO NOTHING", (thread, "role"))
        core.host.thread_id = thread
        run = core.start(read_only=arguments.get("read_only", False))
        if duration is not None:
            run["deadline_at"] = (core.clock() + timedelta(minutes=duration)).isoformat()
            self.store.set_state(core.run_key, run)
        return {"platform": platform, "run": run, "already_running": False}

    def stop_all(self, reason):
        for core in self.cores.values():
            core.stop(reason)
        # Also pause persisted slots whose connector was removed since the last run.
        for platform in PLATFORMS:
            key = "core-run:" + platform
            run = self.store.state(key, {})
            if run.get("status") == "active":
                with self.store.db() as db:
                    self.store.set_state(key, {**run, "status": "paused", "reason": reason}, db)
                    for table in ("core_actions", "actions"):
                        db.execute("UPDATE " + table + " SET status='cancelled' WHERE generation=? AND status='prepared'",
                                   (run["generation"],))
                        db.execute("UPDATE " + table + " SET status='uncertain' WHERE generation=? AND status='dispatching'",
                                   (run["generation"],))

    async def check(self):
        for platform, core in self.cores.items():
            if self.status(platform)["status"] == "active":
                try:
                    if self.service.supervisor and not self.service.supervisor.status()["healthy"]:
                        raise AssistantError("supervisor_lost", "独立监护已退出")
                    await asyncio.to_thread(core.guard)
                except AssistantError as exc:
                    self.fail(platform, exc.code)

    def fail(self, platform, code):
        from .faults import record

        record(self.service, code, run_key=self.cores[platform].run_key)
        self.cores[platform].stop(code)

    def open(self):
        self.jobs = [asyncio.create_task(self.work(platform)) for platform in self.cores]

    async def close(self):
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        for core in self.cores.values():
            for adapter in core.platforms.all():
                close = getattr(adapter, "close", None)
                if close:
                    await close()

    async def work(self, platform):
        from companion_core import delivery

        core = self.cores[platform]
        adapters = core.platforms.all()
        custom_poll = bool(adapters) and all(hasattr(adapter, "runtime_poll") for adapter in adapters)
        generation, next_poll, next_cycle, failures = None, 0, 0, 0
        while not self.service.shutdown.is_set():
            await asyncio.sleep(1)
            try:
                if self.store.state(core.run_key, {}).get("status") != "active":
                    self.busy.add(platform)
                    for adapter in adapters:
                        idle = getattr(adapter, "idle", None)
                        if idle:
                            await idle()
                    continue
                if (self.service.storage_unavailable or self.service.maintenance_until > time.time()
                        or self.status(platform)["status"] != "active"):
                    continue
                if not self.service.platform_hours():
                    continue
                self.busy.add(platform)
                run = await asyncio.to_thread(core.guard)
                if run["generation"] != generation:
                    generation, next_poll, next_cycle, failures = run["generation"], 0, 0, 0
                settings = self.store.read("companion")["data"]
                if time.monotonic() >= next_poll:
                    if custom_poll:
                        incoming = []
                        for adapter in adapters:
                            incoming.extend(await asyncio.wait_for(adapter.runtime_poll(),
                                                                   settings.get("inbox_timeout_seconds", 180)))
                    else:
                        incoming = await asyncio.wait_for(core.poll(), settings.get("inbox_timeout_seconds", 180))
                    failures = 0
                    interval = min((getattr(a, "poll_interval_seconds", settings.get("inbox_poll_seconds", 60))
                                    for a in adapters), default=60)
                    next_poll = time.monotonic() + interval
                    private = incoming if custom_poll else [event for event in incoming if event.visibility == "private"]
                    for event in private[:settings.get("inbox_batch_size", 3)]:
                        if await asyncio.to_thread(core.remaining_model_calls):
                            try:
                                await core.handle(event)
                            except AssistantError as exc:
                                if exc.code not in {"reply_session_expired", "reply_quota_exhausted", "reply_not_authorized"}:
                                    raise
                if not custom_poll and time.monotonic() >= next_cycle:
                    await core.cycle()
                    next_cycle = time.monotonic() + self.store.read("policy")["data"].get("cycle_minutes", 15) * 60
            except (AssistantError, TimeoutError) as exc:
                code = exc.code if isinstance(exc, AssistantError) else "inbox_timeout"
                failures += 1
                next_poll = time.monotonic() + min(900, 60 * 2 ** min(failures, 4))
                if code not in delivery.TEMPORARY | {"cycle_busy"} and (
                        failures >= 3 or code not in {"inbox_timeout", "network_error"}):
                    self.fail_safely(platform, code)
            except Exception:
                self.fail_safely(platform, "unexpected_daemon_error")
            finally:
                self.busy.discard(platform)

    def fail_safely(self, platform, code):
        try:
            self.fail(platform, code)
        except Exception:
            # A shared database outage must not kill this scheduler or allow
            # another run before the monitor's global recovery pause.
            self.service.storage_unavailable = True
