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
        # Explicit web-management authority belongs to this platform host only.
        self.management_owner = None

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
        self.platform_jobs = {}
        self.default_model, self.models = model, models or {}
        for platform in PLATFORMS:
            selected = [adapter for adapter in adapters if adapter.platform == platform]
            if selected:
                self.cores[platform] = Companion(self.store, (models or {}).get(platform, model), BoundHost(service.host), selected,
                                                 run_key="core-run:" + platform)
        if any(adapter.platform not in PLATFORMS for adapter in adapters):
            raise AssistantError("platform_unknown", "平台连接器必须登记为 QQ、微信或 B站")

    def status(self, platform):
        run = self.store.state("core-run:" + platform, {})
        result = {"platform": platform, "available": platform in self.cores,
                "status": run.get("status", "paused"), "reason": run.get(
                    "reason", "running" if run.get("status") == "active" else
                    "explicit_start_required" if platform in self.cores else "platform_unavailable"),
                "owner": run.get("owner"), "generation": run.get("generation"),
                "owner_title": getattr(self.service.host, "chat_title", lambda _: None)(run.get("owner")),
                "read_only": run.get("read_only", False), "deadline_at": run.get("deadline_at"),
                "busy": platform in self.busy}
        if result["reason"] == "outside_active_hours":
            result["reason"] = "running" if result["status"] == "active" else "explicit_start_required"
        fault = self.store.state("platform-fault-stage:" + platform, {})
        if fault.get("code") == result["reason"] and fault.get("generation") == result["generation"]:
            result["fault"] = fault
        if platform == "qq":
            from companion_qq.config import management_url

            result["management_url"] = management_url(getattr(self.service.host, "project", None))
        if run.get("reply_test"):
            result["reply_test"] = run["reply_test"]
        core = self.cores.get(platform)
        if core:
            details = [adapter.runtime_status() for adapter in core.platforms.all()
                       if hasattr(adapter, "runtime_status")]
            if details:
                result["connectors"] = details
                # Lifecycle remains active while a connector retries. Expose
                # transport availability separately so the page cannot claim it is online.
                connection = next((item.get("receiver", item) for item in details
                                   if item.get("receiver", item).get("state") != "connected"),
                                  details[0].get("receiver", details[0]))
                result["connection"] = {"state": connection.get("state", "unknown"),
                                        "code": connection.get("code", "")}
                for item in details:
                    native = item.get("native", {})
                    if native.get("code"):
                        result["connection"] = {"state": "blocked", "code": native["code"]}
                    if item.get("uncertain") or item.get("native_uncertain") or native.get("uncertain"):
                        result["connection"] = {"state": "uncertain", "code": "send_unconfirmed"}
        return result

    def control(self, thread, platform, action, arguments, *, management=False):
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
        self.service.require_runtime()
        if platform in self.service.setup.running:
            raise AssistantError("setup_busy", "该平台正在配置，请等待完成后再启动")
        if not core:
            raise AssistantError("platform_unavailable", "该平台尚未装配可运行连接器，其他平台保持原状态")
        if set(arguments) - {"read_only", "duration_minutes", "reply_test"} or (
                "read_only" in arguments and type(arguments["read_only"]) is not bool):
            raise AssistantError("invalid_control", "平台启动只接受只读模式和运行时长")
        duration = arguments.get("duration_minutes")
        if duration is not None and (type(duration) is not int or not 1 <= duration <= 1440):
            raise AssistantError("invalid_duration", "运行时长须为 1 至 1440 分钟")
        selection = arguments.get("reply_test")
        if selection is not None:
            from companion_core.reply_test import validate

            validate(selection, sorted([a.platform, a.account] for a in core.platforms.all()),
                     read_only=arguments.get("read_only", False))
            if platform != "bilibili" or duration is None or duration > 30:
                raise AssistantError("reply_test_invalid", "限定私信验收仅支持B站，须指定1至30分钟时限")
        service = self.service
        if service.maintenance_until > time.time():
            raise AssistantError("maintenance", "正在准备数据恢复")
        if not self.store.state("web-owner"):
            raise AssistantError("setup_required", "先完成本机网页管理员初始化")
        if service.supervisor and not service.supervisor.status()["healthy"]:
            raise AssistantError("supervisor_lost", "独立监护尚未就绪")
        old = self.store.state(core.run_key, {})
        if old.get("status") == "active":
            if (old["owner"] != thread or old["read_only"] != arguments.get("read_only", False)
                    or old.get("reply_test") != selection):
                raise AssistantError("already_running", "先停止本平台，再改变归属或运行模式")
            return {"platform": platform, "run": old, "already_running": True}
        if platform in self.busy:
            raise AssistantError("worker_busy", "本平台上次任务正在结束")
        with self.store.db() as db:
            row = db.execute("SELECT mode FROM sessions WHERE thread=?", (thread,)).fetchone()
            if row and row["mode"] != "role" and not (
                    management and row["mode"] == "admin" and thread == self.store.state("service-admin-thread")):
                raise AssistantError("mode_conflict", "管理聊天不能接管平台运行")
            db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT DO NOTHING", (thread, "role"))
        previous_owner = core.host.thread_id, core.host.management_owner
        core.host.thread_id = thread
        core.host.management_owner = thread if management and thread == self.store.state("service-admin-thread") else None
        try:
            if selection is None:
                run = core.start(read_only=arguments.get("read_only", False))
            else:
                run = core.start(read_only=False, reply_test=selection)
        except Exception:
            core.host.thread_id, core.host.management_owner = previous_owner
            raise
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
        for platform, core in list(self.cores.items()):
            if self.status(platform)["status"] == "active":
                try:
                    if self.service.supervisor and not self.service.supervisor.status()["healthy"]:
                        raise AssistantError("supervisor_lost", "独立监护已退出")
                    await asyncio.to_thread(core.guard)
                except AssistantError as exc:
                    self.fail(platform, exc.code)

    def fail(self, platform, code):
        from .faults import record

        run = self.store.state(self.cores[platform].run_key, {})
        if (run.get("reply_test") and run.get("status") == "paused"
                and run.get("reason") == "reply_test_complete"):
            # A concurrent guard may observe the stop marker just before the
            # completed test commits its paused state. Keep the intentional end.
            return
        record(self.service, code, run_key=self.cores[platform].run_key)
        self.cores[platform].stop(code)

    def open(self):
        self.platform_jobs = {platform: asyncio.create_task(self.work(platform)) for platform in self.cores}
        self.jobs = list(self.platform_jobs.values())

    async def detach(self, platform):
        core = self.cores.get(platform)
        if core:
            core.stop("platform_configuration_changed")
        job = self.platform_jobs.pop(platform, None)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            if job in self.jobs:
                self.jobs.remove(job)
        if core:
            for adapter in core.platforms.all():
                close = getattr(adapter, "close", None)
                if close:
                    await close()
            self.cores.pop(platform, None)
        self.busy.discard(platform)

    def attach_configured(self, platform):
        if platform == "qq":
            from companion_qq.module import configured_module

            adapter = configured_module(self.store, self.service.host.project)
        else:
            from companion_wechat.module import configured_module

            adapter = configured_module(self.store, self.service.host.project, self.service.installation_id)
        if adapter is not None:
            self.cores[platform] = Companion(self.store, self.models.get(platform, self.default_model),
                BoundHost(self.service.host), [adapter], run_key="core-run:" + platform)
            if self.service.loop is not None:
                job = asyncio.create_task(self.work(platform))
                self.platform_jobs[platform] = job
                self.jobs.append(job)

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
            stage = "idle"
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
                self.busy.add(platform)
                stage = "guard"
                run = await asyncio.to_thread(core.guard)
                if run["generation"] != generation:
                    generation, next_poll, next_cycle, failures = run["generation"], 0, 0, 0
                settings = self.store.read("companion")["data"]
                if time.monotonic() >= next_poll:
                    stage = "inbox"
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
                    from companion_core.reply_batch import handle_batch

                    stage = "reply"
                    await handle_batch(core, private[:settings.get("inbox_batch_size", 3)],
                                       concurrency=2 if custom_poll else 1)
                if self.store.state(core.run_key, {}).get("status") != "active":
                    continue
                if not custom_poll and not run.get("reply_test") and time.monotonic() >= next_cycle:
                    stage = "cycle"
                    await core.cycle()
                    next_cycle = time.monotonic() + self.store.read("policy")["data"].get("cycle_minutes", 15) * 60
            except (AssistantError, TimeoutError) as exc:
                code = exc.code if isinstance(exc, AssistantError) else "inbox_timeout"
                if code.startswith("public_growth_"):
                    from companion_core.observability import label

                    self.store.set_state("platform-fault-stage:" + platform, {
                        "code": code, "stage": label(getattr(exc, "runtime_stage", stage)), "generation": generation})
                failures += 1
                next_poll = time.monotonic() + min(900, 60 * 2 ** min(failures, 4))
                if code not in delivery.TEMPORARY | {"cycle_busy"} and (
                        failures >= 3 or code not in {"inbox_timeout", "network_error"}):
                    self.fail_safely(platform, code)
            except Exception as exc:
                from companion_core.diagnostics import record_exception

                record_exception(self.store, exc, stage=stage, generation=generation, platform=platform)
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
