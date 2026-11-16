"""Private one-off tasks owned by the desktop, never by an autorun platform."""
import asyncio
import re
from types import SimpleNamespace

from companion_core.errors import AssistantError
from companion_core.task_learning import assessment
from companion_core.tasks import ExplicitTasks
from companion_service.task_runner import TaskRunner

RUN = 'core-run:manual'
HANDOFF = 'desktop-manual-handoff'
QUEUE = 'desktop-manual-queue'


class DesktopManualTasks:
    def __init__(self, runtime):
        from .desktop_runtime import OwnedCompanion, RuntimeHost

        self.runtime, self.manager, self.store = runtime, runtime.management, runtime.store
        # A restored installation continues the bound historical local user (D55), so
        # tasks are owned and read as that actor inside its own installation's
        # conversation; without a binding this remains the current owner. Management,
        # the app lock and every account/platform check keep the current owner.
        self.actor = self.manager.room_actor()
        self.conversation = 'desktop-manual:' + self.actor.scope
        self.core = OwnedCompanion(self.store, runtime.platform_model('local'), RuntimeHost(runtime, 'manual'), (), run_key=RUN)
        self.current = None
        self.runner = TaskRunner(SimpleNamespace(store=self.store, core=self.core, admin=self.manager.admin),
                                 private_video=self.read_video)
        self.engine = ExplicitTasks(self.core, self.manager.authority, authenticate=self.authenticate,
            session_active=self.manager._active, handler=self.execute, accepts=self.owns, requests=self.pending_records)
        # A process restart is not permission to repeat an unfinished external read.
        self.engine.stop('application_restarted')
        self.core.stop('application_restarted')
        self.store.set_state(QUEUE, [])

    def pending_records(self):
        return [self.store.read('task_requests', identifier) for identifier in self.store.state(QUEUE, [])]

    def owns(self, data):
        return data['actor'] == self.actor.as_dict() and data['conversation'] == self.conversation

    def authenticate(self, actor, conversation, session):
        if actor != self.actor or conversation != self.conversation:
            raise AssistantError('access_denied', '该任务不属于当前本机用户。')
        return self.manager.room.sessions.request(session, 'role', conversation=self.conversation)

    def request(self, session):
        self.runtime.require_current()
        return self.authenticate(self.actor, self.conversation, session)

    def capabilities(self):
        ready = self.runtime.model_ready('local') and not self.runtime.tasks.blocked
        broker = self.manager.bili
        video = bool(broker and broker._account())
        web = 'web' in getattr(self.runtime.platform_model('local'), 'supported_native', ())
        enabled = self.store.read('companion')['data'].get('native_tools_enabled', True)
        return {'video': {'available': ready and video,
                         'reason': '' if ready and video else '请先登录模型账号和 B 站账号。'},
                **{kind: {'available': ready and web and enabled,
                          'reason': ('是否能实际检索仍取决于当前模型和账号权限。' if ready and web and enabled else
                                     '请先启用模型账号及原生网页工具；当前执行入口需要支持网页检索。')}
                   for kind in ('web', 'learn')},
                'image': {'available': False, 'reason': '当前订阅入口尚未接入生图。'}}

    def require_kind(self, kind):
        capability = self.capabilities().get(kind)
        if capability is None:
            raise AssistantError('task_request', '任务类型无效。')
        if not capability['available']:
            raise AssistantError('model_capability_unavailable', capability['reason'])

    def ensure_run(self):
        self.runtime.tasks.check()
        if not self.runtime.model_ready('local'):
            raise AssistantError('model_not_ready', '请先登录并启用模型账号。')
        return self.core.start()

    def submit(self, session, kind, arguments, nonce):
        request = self.request(session)
        if not isinstance(kind, str):
            raise AssistantError('task_request', '任务类型无效。')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_kind(kind)
            self.ensure_run()
            result = self.engine.submit(request, self.actor, kind, arguments, nonce)
            if not result['duplicate']:
                account = self.manager.bili._account()['uid'] if kind == 'video' else None
                self.store.set_state('desktop-manual-account:' + result['id'], account, db)
                self.store.set_state(QUEUE, self.store.state(QUEUE, [], db) + [result['id']], db)
            return result

    def checked(self, session, identifier):
        request = self.request(session)
        if not isinstance(identifier, str) or not re.fullmatch(r'task-[a-f0-9]{40}', identifier):
            raise AssistantError('task_request', '任务标识无效。')
        data = self.store.read('task_requests', identifier)['data']
        if not self.owns(data):
            raise AssistantError('access_denied', '该任务不属于当前本机用户。')
        return request, data

    def control(self, session, identifier, action, expected):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            request, data = self.checked(session, identifier)
            if action == 'retry':
                self.require_kind(data['kind'])
                self.check_account(data)
                self.ensure_run()
            result = self.engine.control(request, identifier, action, expected)
            if action == 'retry':
                self.store.set_state(QUEUE, sorted(set(self.store.state(QUEUE, []) + [identifier])))
            if action == 'cancel':
                self.store.set_state(QUEUE, [item for item in self.store.state(QUEUE, []) if item != identifier])
                self.cancel_current(identifier)
            return result

    def listing(self, session):
        request = self.request(session)
        items = self.engine.listing(request)[:20]
        # Results are fetched individually; a list cannot multiply 100 KB stages.
        for item in items:
            item['arguments'] = {key: str(value)[:200] for key, value in item['arguments'].items()
                                 if key in ('bvid', 'prompt', 'objective')}
            if item['result']:
                result = item['result']
                grades = result['payload'].get('grades')
                summary = assessment(grades)['summary'] if grades is not None else result['summary']
                item['result'] = {'summary': summary[:800]}
        return {'items': items, 'capabilities': self.capabilities()}

    def detail(self, session, identifier, offset):
        request, _ = self.checked(session, identifier)
        if type(offset) is not int or not 0 <= offset <= 200000:
            raise AssistantError('task_request', '结果页码无效。')
        detail = self.engine.detail(request, identifier)
        result = detail['result']
        text = '尚无已完成步骤。'
        if result:
            payload = result['payload']
            grades = payload.get('grades')
            checked = assessment(grades) if grades is not None else None
            text = checked['summary'] if checked is not None else result['summary']
            if checked is not None:
                text += '\n\n原始逐题评阅（保留模型原文）：'
                for grade in grades:
                    text += ('\n\n题目 ' + grade['id'] + '\n模型原始判定：' +
                             ('通过' if grade['passed'] else '未通过') +
                             '\n评阅理由：' + grade['reason'] + '\n缺口：' + grade['gap'])
                    if grade['id'] in checked['conflicts']:
                        text += '\n矛盾：原始判定为通过，但仍列有缺口；本题不计通过。'
                text += '\n\n评阅局限：' + payload.get('limitations', '')
                text += '\n这不是独立实测或用户掌握证明。'
            if isinstance(payload.get('notes'), list):
                text = '\n\n'.join('第 ' + str(index+1) + ' 段笔记\n' + note
                                     for index, note in enumerate(payload['notes']))
                text += '\n\n读取范围：' + '、'.join(str(start)+'—'+str(end)+' 秒' for start, end in payload['intervals'])
                text += '\n画面模式仅代表采样阅读，不代表完整连续观看。'
            elif isinstance(payload.get('sources'), list):
                text += '\n\n' + '\n\n'.join(source['title']+'\n'+source['url']+'\n'+source['note']
                                               for source in payload['sources'])
                if 'passed' in payload and checked is None:
                    text += '\n\n模型自检：'+('通过' if payload['passed'] else '仍有缺口')+'。这不是独立实测或用户掌握证明。'
        # The native pipe limits escaped JSON to 256 KiB. Keep every page bounded
        # even when all characters are emoji; retain the complete stored result.
        end = min(offset + 8000, len(text))
        return {'text': text[offset:end], 'next': end if end < len(text) else None,
                'result_id': result['id'] if result else None, 'status': detail['status']}

    def check_account(self, task):
        if task['kind'] == 'video':
            expected = self.store.state('desktop-manual-account:' + task['id'])
            broker = self.manager.bili
            if not broker or not expected:
                raise AssistantError('account_changed', '任务的 B 站账号已改变，请重新提交。')
            broker.require_run_account([['bilibili', expected]])

    async def execute(self, task, previous, *, request, guard):
        def check():
            guard()
            self.check_account(task)
        check()
        self.current = (task['id'], asyncio.get_running_loop(), asyncio.current_task())
        try:
            with self.runtime.activity.track('local', 'watching' if task['kind']=='video' else 'learning'):
                return await self.runner(task, previous, request=request, guard=check)
        finally:
            self.current = None

    def cancel_current(self, identifier=None):
        current = self.current
        if current and (identifier is None or identifier == current[0]):
            current[1].call_soon_threadsafe(current[2].cancel)

    def stop(self, reason):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.engine.stop(reason)
            self.store.set_state(QUEUE, [], db)
            self.store.set_state(HANDOFF, [], db)
            self.cancel_current()
            self.core.stop(reason)

    def suspend(self):
        # Saved before waiting for cancellation, so late results cannot commit.
        pending = []
        for row in self.engine._requests():
            identifier = row['data']['id']
            old = self.engine._state(identifier)
            if old['status'] in ('queued', 'running'):
                pending.append(identifier)
                self.engine._save(identifier, {**old, 'status': 'queued', 'error': 'model_account_switch'})
        prior = self.store.state(HANDOFF, [])
        self.store.set_state(HANDOFF, sorted(set(prior + pending)))
        self.core.stop('model_account_switch')

    def resume(self, ready):
        for identifier in self.store.state(HANDOFF, []):
            old = self.engine._state(identifier)
            if old['status'] != 'queued' or old['error'] != 'model_account_switch':
                continue  # A user cancellation always wins over the account handover.
            if ready and self.manager._active(old['session']):
                run = self.core.start()
                self.engine._save(identifier, {**old, 'generation': run['generation'], 'owner': run['owner'], 'error': ''})
            else:
                self.engine._save(identifier, {**old, 'status': 'cancelled', 'error': 'model_account_not_ready'})
        self.store.set_state(HANDOFF, [])

    async def step(self):
        with self.store.lock:
            if not self.store.state(QUEUE, []):
                if self.store.state(RUN, {}).get('status') == 'active':
                    self.core.stop('manual_tasks_finished')
                return False
        try:
            return await self.engine.step()
        finally:
            with self.store.lock:
                queue = self.store.state(QUEUE, [])
                pending = [identifier for identifier in queue
                           if self.engine._state(identifier)['status'] in ('queued', 'running')]
                if pending != queue:
                    self.store.set_state(QUEUE, pending)
                if not pending and self.store.state(RUN, {}).get('status') == 'active':
                    self.core.stop('manual_tasks_finished')

    async def read_video(self, task, previous, *, request, guard, generate):
        from .desktop_bili_connector import adapter
        from .desktop_bili_policy import BiliRuntimeStore
        from .desktop_runtime import OwnedCompanion, RuntimeHost

        guard()
        connector = adapter(self.manager.bili)
        if connector is None:
            raise AssistantError('task_connector', '请先登录 B 站账号。')
        core = OwnedCompanion(BiliRuntimeStore(self.store, connector.account), self.runtime.platform_model('local'),
            RuntimeHost(self.runtime, 'manual-video'), [connector], run_key='core-run:manual-video')
        try:
            run = core.start()
            def check():
                guard()
                core.guard(run['generation'])
            return await connector.read_private_task(task, previous, authority=self.manager.authority,
                                                     request=request, guard=check, generate=generate)
        finally:
            core.stop('manual_stage_finished')
            # SDK sessions are shared by this event loop; the scheduler closes
            # them with the regular Bilibili connector at application shutdown.
