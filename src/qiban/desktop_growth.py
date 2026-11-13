"""Desktop recovery of public growth, using the existing recovery transaction."""
import re
import secrets

from companion_core import public_growth
from companion_core.errors import AssistantError


class DesktopGrowth:
    def __init__(self, management):
        self.management = management
        self.store = management.store
        self.proposal = None

    def require_idle(self, request):
        manager = self.management
        manager.authority.require(request, administrator=True)
        runtime = manager.runtime
        if runtime is not None:
            runtime.require_current()
            if runtime.scheduler is not None:
                state = runtime.scheduler.status('bilibili')
                if state['busy'] or state['stopping']:
                    raise AssistantError('worker_busy', '请先停止 B 站并等待当前任务退出，再检查或恢复公开成长。')
        if self.store.state('core-run:bilibili', {}).get('status') == 'active':
            raise AssistantError('worker_busy', '请先停止 B 站，再检查或恢复公开成长。')

    def preview(self, request):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.proposal = None
            self.require_idle(request)
            value = public_growth.preview(self.store)
            token = secrets.token_urlsafe(24) if value['reason'] != 'readable' else None
            if token:
                self.proposal = {'token': token, 'session': request.session_id, 'expected': value['expected'],
                                 'expires': self.management.clock() + 120}
            return {**value, 'token': token, 'expires_in_seconds': 120 if token else 0}

    def rebuild(self, request, token):
        if not isinstance(token, str):
            raise AssistantError('management_request', '请先检查并预览恢复内容。')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_idle(request)
            proposal, self.proposal = self.proposal, None
            if (not proposal or token != proposal['token'] or request.session_id != proposal['session']
                    or self.management.clock() >= proposal['expires']):
                raise AssistantError('edit_conflict', '恢复预览已失效，请重新检查。')
            result = public_growth.rebuild(self.store, proposal['expected'], request.actor)
            if self.management.runtime is not None:
                from .desktop_runtime import SESSION
                state = self.store.state(SESSION)
                self.store.set_state(SESSION, {**state, 'held': sorted(set(state['held']) | {'bilibili'})}, db)
            return result

    def archives(self, request, after):
        self.management.authority.require(request, administrator=True)
        if not isinstance(after, str) or after and not re.fullmatch(r'public-growth-recovery:[a-f0-9]{32}', after):
            raise AssistantError('management_request', '归档列表位置无效，请重新读取。')
        with self.store.db() as db:
            rows = db.execute("SELECT key FROM state WHERE key LIKE 'public-growth-recovery:%' AND key>? "
                              "ORDER BY key LIMIT 21", (after,)).fetchall()
        self.management.authority.require(request, administrator=True)
        return {'items': [row['key'] for row in rows[:20]], 'next': rows[19]['key'] if len(rows) > 20 else None}

    def archive(self, request, identifier):
        self.management.authority.require(request, administrator=True)
        if not isinstance(identifier, str) or not re.fullmatch(r'public-growth-recovery:[a-f0-9]{32}', identifier):
            raise AssistantError('management_request', '请选择公开成长归档。')
        value = self.store.state(identifier)
        if value is None:
            raise AssistantError('not_found', '归档不存在，请重新读取列表。')
        self.management.authority.require(request, administrator=True)
        return {'id': identifier, 'original': value['original'], 'read_only': True}
