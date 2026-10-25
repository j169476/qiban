"""One persistent owner of the core; HTTP and Codex share these operations."""
from __future__ import annotations

import asyncio
import hmac
import time
from contextvars import ContextVar
from threading import RLock
from zoneinfo import ZoneInfo

from companion_core import events
from companion_core.backup_maintenance import BackupMaintenance
from companion_core.conversation_maintenance import ConversationMaintenance
from companion_core.errors import AssistantError
from companion_core.governance import AdminService
from companion_core.runtime import Companion
from companion_core.sessions import SessionService
from companion_core.tasks import ExplicitTasks

from .local_adapter import LocalAdapter
from .task_runner import TaskRunner


class Service:
    def __init__(self, store, model, host, *, adapters=(), bridge_secret, control_verifier=None, platform_models=None):
        self.store, self.host = store, host
        self.admin = AdminService(store)
        self.sessions = SessionService(self.admin.repository, self.admin.authority)
        self.local = LocalAdapter(store, self.sessions)
        self.core = Companion(store, model, host, [self.local])
        from .platform_runs import PlatformRuns

        self.platform_runs = PlatformRuns(self, model, tuple(adapters), models=platform_models)
        self.compression = ConversationMaintenance(self.core, self.admin.authority)
        self.backups = BackupMaintenance(store)
        self.tasks = ExplicitTasks(self.core, self.admin.authority, authenticate=self._task_authenticate,
                                   session_active=self.admin.repository.session_active, handler=TaskRunner(self))
        self._draining = ContextVar("companion_local_drain", default=False)
        self.core.interactive_checkpoint = self.drain_local
        self._bridge_secret = bridge_secret
        self.control_verifier = control_verifier or host.require_active
        self.wakeup, self.shutdown = asyncio.Event(), asyncio.Event()
        self.task = None
        self.monitor = None
        self.receiver = None
        self.notice_task = None
        self.backup_task = None
        self.backup_active = False
        self.storage_unavailable = False
        self.state_lock = RLock()
        self.busy = False
        self.loop = None
        self.maintenance_until = 0
        self.supervisor = None

    def notify(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.wakeup.set)
        else:
            self.wakeup.set()

    def status(self):
        run = self.store.state("core-run", {})
        return {"status": run.get("status", "paused"), "reason": run.get("reason", "explicit_start_required"),
                "generation": run.get("generation"), "busy": self.busy,
                "platforms": [self.platform_runs.status(p) for p in ("bilibili", "wechat", "qq")],
                "read_only": run.get("read_only", False), "deadline_at": run.get("deadline_at"),
                "heartbeat": self.store.state("service-heartbeat", {}),
                "inbox": self.store.state("service-inbox", {}),
                "supervisor": self.supervisor.status() if self.supervisor else {"separate_process": False}}

    def _pause_local(self, reason):
        with self.state_lock:
            self.core.stop(reason)
            self.tasks.stop(reason)

    def _pause(self, reason):
        with self.state_lock:
            self.core.stop(reason)
            self.platform_runs.stop_all(reason)
            self.tasks.stop(reason)
            old = self.store.state("run", {})
            if old.get("status") == "active":
                self.store.set_state("run", {**old, "status": "paused", "reason": reason})

    async def open(self):
        # Process restart never resumes a run. Existing sessions may survive but
        # cannot cause activity without a fresh, verified Codex start command.
        if self.status()["status"] == "active":
            from .faults import record

            record(self, "daemon_exited")
        self._pause("service_restart")
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self.work())
        self.monitor = asyncio.create_task(self.watch())
        self.receiver = asyncio.create_task(self.receive())
        self.platform_runs.open()

    async def close(self):
        try:
            self._pause("service_shutdown")
        except Exception:
            self.storage_unavailable = True
        self.shutdown.set()
        await self.platform_runs.close()
        self.wakeup.set()
        for task in (self.task, self.monitor, self.receiver):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.task, self.monitor, self.receiver) if task), return_exceptions=True)
        # Cancelling to_thread does not stop its thread. Finish the bounded host
        # notification before the owner closes the database and host client.
        if self.notice_task:
            await asyncio.gather(self.notice_task, return_exceptions=True)
        if self.backup_task:
            await asyncio.gather(self.backup_task, return_exceptions=True)

    def heartbeat(self):
        with self.state_lock:
            if self.supervisor:
                self.supervisor.pulse()
            if self.maintenance_until > time.time():
                return False
            self.store.set_state("service-heartbeat", {"at": time.time(), "busy": self.busy})
            return True

    async def watch(self):
        last_notice = 0
        while not self.shutdown.is_set():
            try:
                self.recover_storage()
                if not self.heartbeat():
                    await asyncio.sleep(1)
                    continue
                await self.platform_runs.check()
                if self.status()["status"] == "active":
                    try:
                        if self.supervisor and not self.supervisor.status()["healthy"]:
                            raise AssistantError("supervisor_lost", "独立监护未就绪或已退出")
                        await asyncio.to_thread(self.core.guard)
                    except AssistantError as exc:
                        from .faults import record

                        record(self, exc.code)
                        self._pause_local(exc.code)
                if (time.monotonic() - last_notice >= 30
                        and (self.notice_task is None or self.notice_task.done())):
                    from .faults import deliver

                    last_notice = time.monotonic()
                    # Notification waits must not delay the independent archive guard.
                    with self.state_lock:
                        if self.maintenance_until <= time.time():
                            self.notice_task = asyncio.create_task(asyncio.to_thread(deliver, self))
            except Exception:
                # Keep the monitor alive during a database outage. The next
                # successful connection pauses activity before doing any work.
                self.storage_unavailable = True
            await asyncio.sleep(2)

    def recover_storage(self):
        if self.storage_unavailable:
            from .faults import record

            self._pause("storage_unavailable")
            record(self, "storage_unavailable")
            self.storage_unavailable = False

    def _finish_expired_local(self, event):
        if self.store.state("web-message:" + event.id, {}).get("reason") == "user_cancelled":
            return
        row = events.get(self.store, event.id)
        self.store.set_state("core-processed:" + event.id, row["version"])
        self.store.set_state("web-message:" + event.id, {"status": "cancelled", "reason": "session_or_run_ended"})

    def platform_hours(self):
        policy = self.store.read("policy")["data"]
        hours = policy.get("active_hours")
        if not hours:
            return True
        now = self.core.clock().astimezone(ZoneInfo(policy.get("timezone", "Asia/Shanghai"))).strftime("%H:%M")
        start, end = hours["start"], hours["end"]
        return start <= now < end if start < end else now >= start or now < end

    async def receive(self):
        next_poll, failures, generation = 0, 0, None
        while not self.shutdown.is_set():
            await asyncio.sleep(1)
            run = self.store.state("core-run", {})
            if run.get("status") != "active" or not self.store.state("service-background", True):
                continue
            if run.get("generation") != generation:
                generation, next_poll, failures = run["generation"], 0, 0
            if time.monotonic() < next_poll or not self.platform_hours():
                continue
            settings = self.store.read("companion")["data"]
            interval = settings.get("inbox_poll_seconds", 60)
            try:
                await asyncio.wait_for(self.core.poll(), settings.get("inbox_timeout_seconds", 180))
                failures = 0
                self.store.set_state("service-inbox", {"at": time.time(), "status": "ready", "failures": 0})
                self.notify()
            except (AssistantError, TimeoutError) as exc:
                code = exc.code if isinstance(exc, AssistantError) else "inbox_timeout"
                if self.status()["status"] != "active":
                    continue
                failures += 1
                self.store.set_state("service-inbox", {"at": time.time(), "status": "backoff",
                                                       "failures": failures, "code": code})
                if failures >= 3 or code in {"login_required", "credential_invalid", "account_mismatch",
                                             "credential_refresh_required", "credential_refresh_uncertain"}:
                    from .faults import record

                    record(self, code if failures < 3 else "inbox_repeated_failure")
                    self._pause_local("inbox_fault")
                interval = min(900, interval * 2 ** failures)
            except Exception:
                from .faults import record

                record(self, "inbox_repeated_failure")
                self._pause_local("inbox_fault")
            next_poll = time.monotonic() + interval

    async def drain_private(self):
        run = self.core.guard()
        if run["read_only"] or not self.store.state("service-background", True) or not self.platform_hours():
            return
        from companion_core import delivery

        accounts = [item for item in run["accounts"] if item[0] != "local"]
        incoming = events.pending(self.store, accounts, run["pending_since"], owner=run["owner"],
                                  now=self.core.clock().isoformat(), config_token=delivery.config_token(self.store))
        for event in [item for item in incoming if item.visibility == "private"][:2]:
            if not self.core.remaining_model_calls():
                break
            await self.core.handle(event)

    async def drain_local(self):
        from companion_core import delivery

        if self._draining.get():
            return
        marker = self._draining.set(True)
        try:
            run = self.core.guard()
            incoming = events.pending(self.store, [["local", self.local.account]], run["pending_since"],
                                      owner=run["owner"], limit=2, now=self.core.clock().isoformat(),
                                      config_token=delivery.config_token(self.store))
            for event in incoming:
                if not self.core.remaining_model_calls():
                    self.store.set_state("web-message:" + event.id, {"status": "deferred", "reason": "model_quota_exhausted"})
                    break
                try:
                    self.store.set_state("web-message:" + event.id, {"status": "processing"})
                    outcome = await self.core.handle(event)
                    action = outcome.get("action") or {}
                    self.store.set_state("web-message:" + event.id, {
                        "status": "deferred" if outcome.get("deferred") else action.get("status", "completed"),
                        "reason": action.get("error", "")})
                except AssistantError as exc:
                    if exc.code == "session_expired":
                        self._finish_expired_local(event)
                    elif exc.code in delivery.TEMPORARY:
                        self.store.set_state("web-message:" + event.id, {
                            "status": "deferred", "reason": exc.code,
                            "due_at": self.store.state(delivery.key(event), {}).get("due_at")})
                    else:
                        self.store.set_state("web-message:" + event.id, {"status": "failed", "reason": exc.code})
                        raise
                except Exception:
                    self.store.set_state("web-message:" + event.id, {"status": "failed", "reason": "internal_error"})
                    raise
        finally:
            self._draining.reset(marker)

    async def work(self):
        next_cycle = 0
        generation = None
        while not self.shutdown.is_set():
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except TimeoutError:
                pass
            self.wakeup.clear()
            try:
                self.recover_storage()
                if self.maintenance_until <= time.time():
                    self.backup_task = asyncio.create_task(asyncio.to_thread(self.maintain_backups))
                    # A shutdown cancels this coroutine, not the file writer.
                    # close() joins the surviving job before closing the store.
                    backup = await asyncio.shield(self.backup_task)
                    if backup and backup.get("failures", 0) >= 3:
                        from .faults import record

                        record(self, "backup_failed", incident=backup["incident"])
                active = self.status()["status"] == "active"
            except Exception:
                # BackupMaintenance retains a bounded retry in memory during a
                # database outage. A failed notification must not kill the worker.
                self.storage_unavailable = True
                continue
            if not active:
                continue
            try:
                self.busy = True
                run = await asyncio.to_thread(self.core.guard)
                if run["generation"] != generation:
                    generation, next_cycle = run["generation"], 0
                await self.drain_local()
                await self.tasks.step()
                await self.drain_private()
                await self.maintain_local()
                if (time.monotonic() >= next_cycle and not self.wakeup.is_set()
                        and self.store.state("service-background", True)):
                    await self.core.cycle()
                    interval = self.store.read("policy")["data"].get("cycle_minutes", 15)
                    next_cycle = time.monotonic() + interval * 60
            except AssistantError as exc:
                # Only stable codes go to operational logs; no prompts or PII.
                if self.status()["status"] != "active":
                    self.tasks.stop(exc.code)
                    continue
                self.store.set_state("service-last-error", {"code": exc.code, "at": time.time()})
                from companion_core.delivery import TEMPORARY

                if exc.code not in TEMPORARY | {"cycle_busy", "session_expired"}:
                    from .faults import record

                    record(self, exc.code)
                    self._pause_local("service_fault")
            except Exception:
                from .faults import record

                record(self, "unexpected_daemon_error")
                self.store.set_state("service-last-error", {"code": "internal_error", "at": time.time()})
                self._pause_local("service_fault")
            finally:
                self.busy = False

    def maintain_backups(self):
        # Restore previews and automatic backup scheduling share this gate.
        with self.state_lock:
            if self.maintenance_until > time.time() or self.backup_active:
                return None
            self.backup_active = True
        try:
            return self.backups.step()
        finally:
            with self.state_lock:
                self.backup_active = False

    async def maintain_local(self):
        if self.wakeup.is_set() or self.status()["status"] != "active":
            return None
        request = self.local.maintenance_request()
        if request is None:
            return None
        result = await self.compression.step(request, account=self.local.account, channel=self.local.platform,
            authenticate=lambda: self.sessions._recover(request.session_id, "role", conversation=request.conversation))
        if result and result.get("failures", 0) >= 3:
            from .faults import record

            record(self, "private_compression_failed")
            self._pause_local("private_compression_failed")
        return result

    def control(self, secret, thread, operation, arguments):
        with self.state_lock:
            return self._control(secret, thread, operation, arguments)

    def _control(self, secret, thread, operation, arguments):
        if not isinstance(arguments, dict) or not isinstance(operation, str):
            raise AssistantError("invalid_control", "控制参数无效")
        if not isinstance(secret, str) or not hmac.compare_digest(secret, self._bridge_secret):
            raise AssistantError("bridge_denied", "控制入口未认证")
        if not isinstance(thread, str) or not 1 <= len(thread) <= 200:
            raise AssistantError("bridge_denied", "缺少宿主聊天标识")
        self.control_verifier(thread)  # Official host checks project and archive state.
        if operation == "management_open":
            with self.store.db() as db:
                row = db.execute("SELECT mode FROM sessions WHERE thread=?", (thread,)).fetchone()
                if row and row["mode"] != "admin":
                    raise AssistantError("mode_conflict", "此聊天已绑定为角色控制入口")
                db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT DO NOTHING", (thread, "admin"))
                self.store.set_state("service-admin-thread", thread, db)
                self.store.set_state("management_thread", thread, db)
            return {"bound": True, "role_started": False}
        for prefix, platform in (("bili", "bilibili"), ("wechat", "wechat"), ("qq", "qq")):
            if operation.startswith(prefix + "_") and operation.split("_", 1)[1] in {"start", "stop", "pause", "status"}:
                return self.platform_runs.control(thread, platform, operation.split("_", 1)[1], arguments)
        if operation == "assistant_status":
            return self.status()
        if operation == "assistant_start":
            if self.supervisor and not self.supervisor.status()["healthy"]:
                raise AssistantError("supervisor_lost", "独立监护未就绪，请稍后再显式启动")
            if (set(arguments) - {"read_only", "background", "duration_minutes"}
                    or any(type(arguments[key]) is not bool for key in ("read_only", "background") if key in arguments)):
                raise AssistantError("invalid_control", "运行模式必须为布尔值，且不能包含其他控制参数")
            if self.maintenance_until > time.time():
                raise AssistantError("maintenance", "正在准备数据恢复，暂不能启动")
            if not self.store.state("web-owner"):
                raise AssistantError("setup_required", "先完成本机网页管理员初始化")
            if self.busy:
                raise AssistantError("worker_busy", "上次任务正在停止，请稍后显式启动")
            if arguments.get("duration_minutes") is not None:
                duration = arguments["duration_minutes"]
                if type(duration) is not int or not 1 <= duration <= 1440:
                    raise AssistantError("invalid_duration", "运行时长须为 1 至 1440 分钟")
            old = self.store.state("core-run", {})
            if old.get("status") == "active":
                if (old.get("owner") != thread or old.get("read_only") != arguments.get("read_only", False)
                        or self.store.state("service-background", True) != arguments.get("background", True)):
                    raise AssistantError("already_running", "先停止当前运行，再显式改变归属或模式")
                return {"run": old, "conversation_location": "local_web", "already_running": True}
            with self.store.db() as db:
                row = db.execute("SELECT mode FROM sessions WHERE thread=?", (thread,)).fetchone()
                if row and row["mode"] != "role":
                    raise AssistantError("mode_conflict", "管理聊天不能接管角色运行")
                db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT DO NOTHING", (thread, "role"))
            self.host.thread_id = thread
            self.tasks.stop("run_replaced")
            result = self.core.start(read_only=arguments.get("read_only", False))
            self.store.set_state("service-background", arguments.get("background", True))
            if arguments.get("duration_minutes") is not None:
                from datetime import timedelta

                result["deadline_at"] = (self.core.clock() + timedelta(minutes=arguments["duration_minutes"])).isoformat()
                self.store.set_state("core-run", result)
            self.notify()
            return {"run": result, "conversation_location": "local_web"}
        if operation in {"assistant_stop", "assistant_pause"}:
            if thread not in (self.store.state("service-admin-thread"), self.store.state("core-run", {}).get("owner")):
                raise AssistantError("not_owner", "只有归属控制聊天或后台管理聊天可以停止")
            self.core.stop("explicit_stop")
            self.tasks.stop("explicit_stop")
            return self.status()
        if thread != self.store.state("service-admin-thread"):
            raise AssistantError("admin_required", "请先绑定后台管理聊天")
        if operation == "assistant_stop_all":
            if arguments:
                raise AssistantError("invalid_control", "全部停止不接受参数")
            self._pause("explicit_stop_all")
            return self.status()
        from companion_core.contracts import ActorRef

        actor = ActorRef("codex-control", thread)
        request = self.admin.authority.issue(actor=actor, conversation="management", audience="private",
                                             recipients=(actor,), purpose="management", administrator=True)
        if operation in {"reply_authorization_list", "reply_authorization_set", "reply_policy_list", "reply_policy_set"}:
            from .reply_management import ReplyManagement

            return ReplyManagement(self).operation(request, operation, arguments)
        if operation == "config_get":
            if arguments.get("kind") not in {"policy", "companion"}:
                raise AssistantError("web_management_required", "此资料请在已登录的管理网页查看")
            return self.admin.read_config(request, arguments["kind"])
        if operation == "config_patch":
            if arguments.get("kind") not in {"policy", "companion"}:
                raise AssistantError("web_management_required", "此资料请在已登录的管理网页修改")
            return self.admin.patch_config(request, arguments["kind"], arguments["changes"], arguments["expected_version"])
        if operation == "setup_ticket":
            return {"ticket": self.sessions.setup_ticket()}
        if operation == "password_reset_ticket":
            self._pause("password_reset")
            return {"ticket": self.sessions.reset_ticket()}
        raise AssistantError("web_entry_required", "角色内容已移至本机网页，此工具不能读取角色资料")

    def _task_conversation(self, actor):
        from companion_core.contracts import Event

        return Event("local", self.local.account, "task", actor, actor.key, "", "").thread

    def task_context(self, token):
        initial = self.sessions.request(token, "role")
        actor = self.sessions.actor(initial)
        return self.sessions.request(token, "role", conversation=self._task_conversation(actor)), actor

    def _task_authenticate(self, actor, conversation, session):
        request = self.sessions._recover(session, "role", conversation=self._task_conversation(actor))
        if request.actor != actor.key or conversation != request.conversation:
            raise AssistantError("access_denied", "任务的原始主体或会话已改变")
        return request

    async def control_async(self, secret, thread, operation, arguments):
        if not operation.startswith("login_browser_"):
            return await asyncio.to_thread(self.control, secret, thread, operation, arguments)
        await asyncio.to_thread(self.control, secret, thread, "assistant_status", {})
        if thread != self.store.state("service-admin-thread"):
            raise AssistantError("admin_required", "浏览器登录只能在已绑定管理聊天进行")
        if (self.status()["status"] == "active"
                or self.platform_runs.status("bilibili")["status"] == "active"):
            raise AssistantError("login_requires_pause", "先暂停角色，再更新平台凭据")
        from bili_assistant import browser_login
        from bili_assistant.engine import Engine

        parent = self.host

        class BoundHost:
            def identity(self):
                return thread

            def require_active(self, thread_id=None):
                return parent.require_active(thread_id or thread)

        engine = Engine(self.store, BoundHost())
        if operation == "login_browser_finish":
            return await browser_login.finish(engine, **arguments)
        handlers = {"login_browser_prepare": browser_login.prepare, "login_browser_status": browser_login.status,
                    "login_browser_cancel": browser_login.cancel}
        if operation not in handlers:
            raise AssistantError("unknown_tool", "未知浏览器登录操作")
        return await asyncio.to_thread(handlers[operation], engine, **arguments)
