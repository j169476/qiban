"""Application-owned run identity and lifecycle, independent of Codex chats."""
import secrets
import uuid

from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.runtime import Companion
from companion_core.runtime_keys import DESKTOP_PLATFORMS as PLATFORMS
from qiban.desktop_identity import ROLE, verify_role
from qiban.desktop_tasks import TaskBarrier

PREFERENCES = 'desktop-autorun'
SESSION = 'desktop-runtime-session'
# A cross-machine restore leaves this installation holding every platform until the user
# starts each one. It is durable, so a later application session cannot resume them, unlike
# SESSION.held, which only remembers an explicit stop inside one application session.
HOLD = 'desktop-portable-hold'


def provision_runtime(store, installation, db):
    if not store.state(ROLE,db=db):
        store.set_state(ROLE,ActorRef('qiban-role',uuid.uuid4().hex,installation).as_dict(),db)
    if not store.state(PREFERENCES,db=db):
        store.set_state(PREFERENCES,{'revision':secrets.token_hex(16),'platforms':{p:False for p in PLATFORMS},
                                    'accounts':{p:[] for p in PLATFORMS}},db)


class RuntimeHost:
    def __init__(self, runtime, platform):
        self.runtime = runtime
        self.owner = 'qiban-run:'+runtime.instance+':'+platform

    def identity(self):
        return self.owner

    def require_active(self, owner=None):
        self.runtime.require_current()
        if owner is not None and owner!=self.owner:
            raise AssistantError('stale_runtime','该运行实例已失效')


class OwnedCompanion(Companion):
    def __init__(self, store, model, host, adapters, *, run_key):
        if run_key=="core-run:bilibili" and host.runtime.management.bili and len(adapters)==1:
            from .desktop_bili_policy import BiliRuntimeStore
            store = BiliRuntimeStore(store,adapters[0].account)
        super().__init__(store,model,host,adapters,run_key=run_key)

    def start(self, *, read_only=False, reply_test=None):
        with self.store.lock:
            if self.run_key=="core-run:bilibili" and self.host.runtime.management.bili:
                self.host.runtime.management.bili.require_run_account(
                    sorted([adapter.platform,adapter.account] for adapter in self.platforms.all()))
            return super().start(read_only=read_only,reply_test=reply_test)

    async def _handle_serial(self, event):
        adapter = self.platforms.get(event.platform, event.account)
        work = super()._handle_serial
        if hasattr(adapter, "run_cancellable_reply"):
            return await adapter.run_cancellable_reply(event, work)
        return await work(event)

    def guard(self, generation=None):
        self.host.runtime.tasks.check()
        self.host.runtime.require_current()
        run = super().guard(generation)
        if self.run_key=="core-run:bilibili" and self.host.runtime.management.bili:
            self.host.runtime.management.bili.require_run_account(run["accounts"])
        return run

    async def cycle(self):
        return await self.host.runtime.tasks.run(super().cycle)

    async def poll(self):
        return await self.host.runtime.tasks.run(super().poll)

    async def handle(self, event):
        work = super().handle
        return await self.host.runtime.tasks.run(lambda: work(event))

    async def model(self, *args, **kwargs):
        work = super().model
        return await self.host.runtime.tasks.run(lambda: work(*args, **kwargs))

    async def dispatch(self, *args, **kwargs):
        work = super().dispatch
        return await self.host.runtime.tasks.run(lambda: work(*args, **kwargs))

    async def prepare_backlog(self, *args, **kwargs):
        work = super().prepare_backlog
        return await self.host.runtime.tasks.run(lambda: work(*args, **kwargs))

    def stop(self, reason='user_stop', *, generation=None):
        self.host.runtime.require_current()
        return super().stop(reason,generation=generation)


class DesktopRuntime:
    """Construct only inside the native-owned worker; adapters/models are injected."""
    def __init__(self, management, application_session, *, model=None, adapters=(), room_model=None):
        if not isinstance(application_session,str) or not application_session or len(application_session)>120:
            raise AssistantError('runtime_identity','应用会话标识无效')
        self.management,self.store = management,management.store
        self.application,self.instance = application_session,uuid.uuid4().hex
        role = self.store.state(ROLE)
        if not role:
            raise AssistantError('runtime_identity','本机角色身份尚未建立')
        # Exact installation-scoped role; a logical identity binding must match too.
        self.role = verify_role(self.store, role, installation=management.installation,
                                owner=management.actor)
        self.model,self.closed = model,False
        self.room_model = room_model
        self.tasks = TaskBarrier()
        self.scheduler = None
        self.reconfiguring = set()
        self.cores = {}
        for platform in PLATFORMS:
            selected = [a for a in adapters if a.platform==platform]
            if selected:
                executor = self.platform_model(platform)
                self.cores[platform] = OwnedCompanion(self.store,executor,RuntimeHost(self,platform),selected,run_key='core-run:'+platform)
        if any(a.platform not in PLATFORMS for a in adapters):
            raise AssistantError('platform_unknown','平台连接器尚未登记')
        with self.store.lock,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.store.state(SESSION,{},db)
            prefs = self.store.state(PREFERENCES,db=db)
            if "local" not in prefs["platforms"]:
                prefs["platforms"]["local"] = False
                prefs["accounts"]["local"] = []
                prefs["revision"] = secrets.token_hex(16)
                self.store.set_state(PREFERENCES,prefs,db)
            held = previous.get('held',[]) if previous.get('application')==self.application else []
            # A durable restore hold outlives this application session: the user releases
            # each platform by starting it, never by restarting the application.
            held = sorted(set(held) | set(self.held_platforms(db)))
            session = {'application':self.application,'instance':self.instance,'held':held}
            if previous.get('account_switch', {}).get('status') in ('stopping', 'pending'):
                # A crash cannot reopen admission on the old account mid-cutover.
                self.tasks.block()
                session.update(held=previous.get('held', []), account_switch=previous['account_switch'])
            self.store.set_state(SESSION,session,db)
            # A previous worker's active bit cannot authorize continuing its actions.
            for platform in PLATFORMS:
                run = self.store.state('core-run:'+platform,{},db)
                if run.get('status')=='active':
                    management.notifications.record('runtime_restarted',run['generation'],platform)
                self._pause(platform,'runtime_restarted',db)
            for platform in ('manual', 'manual-video'):
                self._pause(platform, 'runtime_restarted', db)
        from companion_core.task_memory_repair import repair
        repair(self.store)
        management.runtime = self
        from .desktop_manual_tasks import DesktopManualTasks
        self.manual = DesktopManualTasks(self)
        if management.codex is not None and hasattr(management.codex, 'account_change'):
            management.codex.account_change = self.change_model_account

    def require_current(self):
        state = self.store.state(SESSION,{})
        if self.closed or state.get('instance')!=self.instance or state.get('application')!=self.application:
            raise AssistantError('stale_runtime','该后台运行实例已退出或被替换')

    def held_platforms(self, db=None):
        """Platforms a restore still holds; a malformed record refuses instead of opening."""
        value = self.store.state(HOLD, None, db)
        if value is None:
            return []
        if (not isinstance(value, dict) or set(value) != {'format', 'generation', 'platforms', 'at'}
                or type(value['format']) is not int or value['format'] != 1
                or not isinstance(value['platforms'], list) or len(set(value['platforms'])) != len(value['platforms'])
                or any(platform not in PLATFORMS for platform in value['platforms'])):
            raise AssistantError('runtime_hold_invalid', '资料恢复后的平台停止记录无效，请人工检查')
        return list(value['platforms'])

    def release_hold(self, platform, db=None):
        """Only an explicit user start of that platform ends its durable restore hold."""
        held = self.held_platforms(db)
        if platform not in held:
            return
        value = self.store.state(HOLD, {}, db)
        self.store.set_state(HOLD, {**value, 'platforms': [item for item in held if item != platform]}, db)

    def open(self):
        from .desktop_scheduler import DesktopScheduler

        self.require_current()
        if self.scheduler is not None:
            raise AssistantError('scheduler_started', '后台任务调度已启动。')
        self.scheduler = DesktopScheduler(self, maintenance=self.maintain)
        self.scheduler.open()

    def maintain(self):
        from companion_core.fact_retirement import cleanup
        from companion_core.media_originals import MediaOriginals

        MediaOriginals(self.store).cleanup(guard=self.require_current)
        with self.tasks.idle_maintenance() as idle:
            if idle:
                cleanup(self.store, guard=self.require_current, reclaim_snapshots=True)

    def configure_platform(self, platform, adapter):
        """Replace a stopped connector without disturbing other platforms."""
        if platform not in PLATFORMS or adapter is not None and adapter.platform != platform:
            raise AssistantError('platform_unknown', '平台连接器不匹配。')
        with self.store.lock:
            self.require_current()
            if self.store.state('core-run:'+platform, {}).get('status') == 'active':
                raise AssistantError('platform_stop_required', '请先停止该平台，再修改连接设置。')
            self.reconfiguring.add(platform)
            state = self.store.state(SESSION)
            self.store.set_state(SESSION, {**state, 'held':sorted(set(state['held']) | {platform})})

        def commit():
            with self.store.lock:
                self.require_current()
                if adapter is None:
                    self.cores.pop(platform, None)
                else:
                    self.cores[platform] = OwnedCompanion(self.store, self.platform_model(platform), RuntimeHost(self, platform),
                                                         [adapter], run_key='core-run:'+platform)
                self.reconfiguring.discard(platform)

        if self.scheduler is None:
            commit()
        else:
            self.scheduler.replace_platform(platform, commit)

    def configure_qq(self):
        from .desktop_qq_targets import adapter

        self.configure_platform('qq', adapter(self.management.qq))

    def fail(self, platform, code, generation):
        """A failed old task cannot stop a newly selected run."""
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_current()
            run = self.store.state('core-run:'+platform, {}, db)
            if run.get('status') != 'active' or run.get('generation') != generation:
                return
            self._pause(platform, code, db)
            self.cores[platform].stop(code, generation=generation)
            state = self.store.state(SESSION, db=db)
            self.store.set_state(SESSION, {**state, 'held':sorted(set(state['held']) | {platform})}, db)
            self.management.notifications.record(code, generation, platform)

    def _pause(self, platform, reason, db):
        key = 'core-run:'+platform
        run = self.store.state(key,{},db)
        if run.get('status')=='active':
            self.store.set_state(key,{**run,'status':'paused','reason':reason},db)
            for table in ('core_actions','actions'):
                db.execute('UPDATE '+table+" SET status='cancelled' WHERE generation=? AND status='prepared'",(run['generation'],))
                db.execute('UPDATE '+table+" SET status='uncertain' WHERE generation=? AND status='dispatching'",(run['generation'],))

    def status(self):
        self.require_current()
        preferences = self.store.state(PREFERENCES)
        session = self.store.state(SESSION)
        platforms=[]
        for platform in PLATFORMS:
            run = self.store.state('core-run:'+platform,{})
            available = platform in self.cores
            platforms.append({'platform':platform,'available':available,'model_ready':self.model_ready(platform),
                'autorun':preferences['platforms'][platform],'held':platform in session['held'],
                'status':run.get('status','paused'),'reason':run.get('reason','explicit_start_required' if available else 'platform_unavailable'),
                'generation':run.get('generation'),'owner':run.get('owner'),'read_only':run.get('read_only',False)})
            if self.scheduler is not None:
                platforms[-1].update(self.scheduler.status(platform))
        return {'role':self.role.as_dict(),'application_session':self.application,'runtime_instance':self.instance,
                'preference_revision':preferences['revision'],'platforms':platforms,'tasks_blocked':self.tasks.blocked,
                'handoff_pending':self.handoff_pending()}

    def platform_model(self, platform):
        if platform in ("local","bilibili") and self.room_model is not None:
            return self.room_model
        return self.model

    def model_ready(self, platform=None):
        broker = self.management.codex
        available = self.platform_model(platform) is not None
        return available and (broker is None or not hasattr(broker, 'ready_for_model') or broker.ready_for_model())

    def handoff_pending(self):
        return (self.tasks.blocked and not self.closed and
                self.store.state(SESSION, {}).get('account_switch', {}).get('status') in ('stopping', 'pending'))

    def preference(self, request, platform, enabled, expected):
        self.management.authority.require(request,administrator=True)
        if platform not in PLATFORMS or type(enabled) is not bool or not isinstance(expected,str):
            raise AssistantError('runtime_request','自动运行设置无效')
        with self.store.lock,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_current()
            self.management.authority.require(request,administrator=True)
            old = self.store.state(PREFERENCES,db=db)
            if old['revision']!=expected:
                raise AssistantError('edit_conflict','自动运行设置已改变，请重新读取')
            if enabled and platform not in self.cores:
                raise AssistantError('platform_unavailable','请先接入并核对平台账号，再开启自动运行')
            accounts=sorted(a.account for a in self.cores[platform].platforms.all()) if enabled else []
            value={'revision':secrets.token_hex(16),'platforms':{**old['platforms'],platform:enabled},
                   'accounts':{**old['accounts'],platform:accounts}}
            self.store.set_state(PREFERENCES,value,db)
            self.management.repository.audit(request.actor,'desktop_autorun_preference',platform,old['revision'],value['revision'],db=db)
        return self.status()

    def start(self, platform, *, automatic=False, read_only=False):
        self.tasks.check()
        if self.scheduler is not None:
            scheduling = self.scheduler.status(platform)
            if scheduling['scheduler_error']:
                raise AssistantError('scheduler_failed', '后台任务调度已停止，请重启应用后台。')
            if scheduling['stopping']:
                raise AssistantError('worker_busy', '该平台上次任务仍在退出，请稍候再启动。')
        if platform not in PLATFORMS:
            raise AssistantError('platform_unknown','未知平台')
        with self.store.lock:
            self.require_current()
            if platform in self.reconfiguring:
                raise AssistantError('platform_reconfigure_pending', '连接器正在更新，请稍候。')
            if platform == 'wechat' and self.management.wechat_resources and platform in self.cores:
                from .desktop_wechat import _profile, selected_collector, selection
                current = selected_collector(self.store, self.management.installation)
                login = selection(self.store, self.management.installation)["selected"]
                _, key = _profile(self.store, self.management.installation, login)
                revision = self.store.state(key, {}).get("revision", 0)
                if any(a.account != current or getattr(a,"setup_revision",0) != revision
                       for a in self.cores[platform].platforms.all()):
                    raise AssistantError('platform_reconfigure_pending', '微信账号已切换，请重新加载连接器。')
            if platform == 'qq' and self.management.qq is not None:
                qq = self.management.qq
                if qq.thread is not None and qq.thread.is_alive():
                    raise AssistantError('qq_login_busy', 'QQ 账号或会话核验仍在进行，请等待或取消。')
                if qq.closed or qq.worker is not None:
                    raise AssistantError('qq_cleanup_failed', 'QQ 账号后台未能正常退出，请重启应用后台后再试。')
                if platform in self.cores:
                    from .desktop_qq import KEY
                    from .desktop_qq_targets import saved_options

                    options = saved_options(qq, self.store.state(KEY, {'revision':0, 'selected':None}))
                    if any(getattr(a, 'options', None) != options for a in self.cores[platform].platforms.all()):
                        raise AssistantError('platform_reconfigure_pending', 'QQ 设置已改变，请重新加载连接器。')
            state = self.store.state(SESSION)
            if automatic and (not self.store.state(PREFERENCES)['platforms'][platform] or platform in state['held']):
                return {'started':False,'reason':'explicit_stop' if platform in state['held'] else 'autorun_disabled'}
            if platform not in self.cores:
                raise AssistantError('platform_unavailable','该平台连接器尚未接入')
            if automatic and sorted(a.account for a in self.cores[platform].platforms.all())!=self.store.state(PREFERENCES)['accounts'][platform]:
                raise AssistantError('account_changed','账号已改变，请核对后重新设置自动运行')
            if not self.model_ready(platform):
                raise AssistantError('model_unavailable','模型尚未就绪，未启动平台')
            run = self.cores[platform].start(read_only=read_only)
            if not automatic and platform in state['held']:
                self.store.set_state(SESSION,{**state,'held':[p for p in state['held'] if p!=platform]})
            # The user starting this platform is the only thing that ends a restore hold.
            self.release_hold(platform)
            if self.scheduler is not None:
                self.scheduler.wake(platform)
            return {'started':True,'run':run,'role':self.role.as_dict()}

    def reconcile(self):
        """Trusted worker calls this when startup/readiness changes; never retries a send."""
        results={}
        for platform in PLATFORMS:
            try:
                results[platform]=self.start(platform,automatic=True)
            except AssistantError as error:
                self.management.notifications.record(error.code,self.instance,platform)
                results[platform]={'started':False,'reason':error.code}
        return results

    def control(self, request, platform, action):
        self.management.authority.require(request,administrator=True)
        if platform not in PLATFORMS or not (action in ('start','stop') or platform=='bilibili' and action=='start_read_only'):
            raise AssistantError('runtime_request','平台操作无效')
        self.require_current()
        if action == 'stop' and platform in self.cores:
            self.cores[platform].interrupt_transports()
        with self.store.lock,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_current()
            self.management.authority.require(request,administrator=True)
            if action in ('start','start_read_only'):
                result = self.start(platform,read_only=action=='start_read_only')
            else:
                state = self.store.state(SESSION,db=db)
                self.store.set_state(SESSION,{**state,'held':sorted(set(state['held'])|{platform})},db)
                self._pause(platform,'explicit_stop',db)
                if platform in self.cores:
                    self.cores[platform].stop('explicit_stop')
                if self.scheduler is not None:
                    self.scheduler.cancel(platform)
                result = {'stopped':True,'autorun_preserved':True}
            self.management.repository.audit(request.actor,'desktop_platform_'+action,platform,db=db)
        return result

    def change_model_account(self, commit, *, ready):
        """Cancel all old work, commit the account, then re-enter through ledgers.

        Runs on the account operation thread, never on a task's asyncio loop.
        Failure leaves admission closed; no old-account rollback or implicit retry.
        """
        self.require_current()
        attempts = self.tasks.block()
        for core in self.cores.values():
            core.interrupt_transports()
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_current()
            self.manual.suspend()
            state = self.store.state(SESSION, db=db)
            prior = state.get('account_switch', {})
            resume = prior.get('resume', {}) if prior.get('status') in ('stopping', 'pending') else {}
            for platform in PLATFORMS:
                run = self.store.state('core-run:'+platform, {}, db)
                if run.get('status') == 'active':
                    resume[platform] = {'read_only':run.get('read_only', False), 'reply_test':run.get('reply_test'),
                                        'pending_since':run.get('pending_since', run['started_at']),
                                        'deadline_at':run.get('deadline_at'), 'accounts':run['accounts']}
                    if platform == 'local':
                        resume[platform]['room_intake'] = run.get('room_intake',run['generation'])
                self._pause(platform, 'model_account_switch', db)
                if platform in self.cores:
                    self.cores[platform].stop('model_account_switch')
            self.store.set_state(SESSION, {**state, 'account_switch':{'status':'stopping', 'resume':resume}}, db)
        self.tasks.drain(attempts)
        result = commit()
        # The account change is durable before any new-generation task can run.
        try:
            with self.store.lock, self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                self.require_current()
                state = self.store.state(SESSION, db=db)
                resumed = []
                self.manual.resume(ready and self.model_ready('local'))
                if ready:
                    for platform, options in resume.items():
                        if platform not in state['held'] and platform in self.cores and self.model_ready(platform):
                            try:
                                accounts = sorted([a.platform, a.account] for a in self.cores[platform].platforms.all())
                                if accounts != options['accounts']:
                                    raise AssistantError('account_changed', '平台账号已改变，请重新确认运行。')
                                run = self.cores[platform].start(read_only=options['read_only'], reply_test=options['reply_test'])
                                run['pending_since'] = options['pending_since']
                                if platform == 'local' and options.get('room_intake'):
                                    run['room_intake'] = options['room_intake']
                                if options['deadline_at']:
                                    run['deadline_at'] = options['deadline_at']
                                self.store.set_state('core-run:'+platform, run, db)
                                resumed.append(platform)
                            except AssistantError as error:
                                self.management.notifications.record(error.code, self.instance, platform)
                self.store.set_state(SESSION, {**state, 'account_switch':{
                    'status':'complete', 'resumed':resumed}}, db)
        except Exception:
            raise AssistantError('model_account_handoff_pending', '账号已保存，但任务恢复未完成；旧任务保持停止，请重新选择账号。') from None
        self.tasks.release()
        if self.scheduler is not None:
            for platform in self.cores:
                self.scheduler.wake(platform)
        return result

    def close(self):
        if self.closed:
            return
        self.require_current()
        self.tasks.block()
        if self.scheduler is not None:
            self.scheduler.stop_requested.set()
        for core in self.cores.values():
            core.interrupt_transports()
        # A native submit can hold the ledger lock. Interrupt it before manual
        # queue cleanup acquires that lock, just as explicit stop/cutover do.
        self.manual.stop('application_exit')
        try:
            self.tasks.close()
        finally:
            if self.scheduler is not None:
                self.scheduler.close()
        with self.store.lock,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_current()
            state = self.store.state(SESSION,db=db)
            self.store.set_state(SESSION,{**state,'held':list(PLATFORMS)},db)
            for platform in PLATFORMS:
                self._pause(platform,'application_exit',db)
                if platform in self.cores:
                    self.cores[platform].stop('application_exit')
        self.closed=True

    def stop_all(self, request):
        self.management.authority.require(request, administrator=True)
        self.manual.stop('explicit_stop')
        for platform in PLATFORMS:
            self.control(request, platform, 'stop')
        return {'stopped': True, 'autorun_preserved': True}

    def preserve_after_restore(self, db):
        """Keep this application session only; the restore settles role and binding.

        `desktop_backups.complete()` writes back the role and logical identity read
        before the snapshot rows were copied, so nothing here may re-derive the role
        from a runtime that may predate a later rebinding.
        """
        self.store.set_state(SESSION,{'application':self.application,'instance':self.instance,'held':list(PLATFORMS)},db)
        self.manual.stop('restore')
        from companion_core.task_memory_repair import STATE, repair
        # Restore may bring back an old generated summary. Recheck before the
        # restore transaction commits or any model task can read its memories.
        self.store.set_state(STATE, {'after': '', 'scanned': 0, 'corrected': 0, 'complete': False}, db)
        repair(self.store)
        GovernanceRepository(self.store).audit(self.role.key,'desktop_runtime_restore_hold','all_platforms',db=db)
