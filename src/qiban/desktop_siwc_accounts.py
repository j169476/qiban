"""Saved SIWC registrations with explicit selection; never quota-based failover."""
import hashlib

from companion_core.errors import AssistantError
from qiban.desktop_siwc import ISSUER, KEY, DesktopSiwc


def registration_id(value):
    return hashlib.sha256((ISSUER+'\0'+value['client_id']+'\0'+value['subject']).encode()).hexdigest()[:32]


def catalog(value):
    """Read the previous single-registration format without rewriting on inspection."""
    if value.get('format') == 2:
        if not isinstance(value.get('accounts'), dict) or value.get('active') not in (None, *value['accounts']):
            raise ValueError('Invalid account catalog')
        return value
    if 'format' in value:
        raise ValueError('Unsupported account catalog version')
    result = {'format':2, 'revision':0, 'host':value.get('host'), 'active':None, 'accounts':{}}
    if value.get('client_id') and value.get('subject'):
        identifier = registration_id(value)
        result['accounts'][identifier] = {**value, 'number':1}
        result['active'] = identifier
    return result


def selected(value):
    book = catalog(value)
    return book['accounts'].get(book['active'], {})


class DesktopSiwcAccounts(DesktopSiwc):
    operations = (*DesktopSiwc.operations, 'add', 'select')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = None
        self.adding = False
        self.selection = None
        self.view = {}
        self.account_change = lambda commit, *, ready: commit()
        self._update_view()

    def _book(self):
        with self.store.lock, self.store.db() as db:
            return catalog(self.store.state(KEY, db=db) or {})

    def _update_view(self):
        book = self._book()
        rows = []
        for identifier, account in book['accounts'].items():
            rows.append({'id':identifier, 'label':f"账号 {account['number']} · {account.get('email') or 'ChatGPT'}",
                         'signed_in':bool(account.get('session')),
                         'active':identifier == book['active']})
        with self.lock:
            self.view = {'accounts':rows, 'active_account':book['active'], 'accounts_revision':book['revision']}

    def status(self):
        with self.lock:
            return {**super().status(), **self.view, 'multi_account':True}

    def begin(self, operation, *, account_id=None, expected=None):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise AssistantError('model_busy','账号操作仍在进行，请完成或取消后再试')
            if operation == 'select':
                if (not isinstance(account_id,str) or len(account_id)!=32 or
                        type(expected) is not int or expected < 0):
                    raise AssistantError('model_account_selection','请选择已保存的账号')
                self.selection = (account_id, expected)
            return super().begin(operation)

    def _run(self, operation):
        try:
            super()._run(operation)
        finally:
            try:
                self._update_view()
            except Exception:
                self._set(state='error',code='model_accounts_unavailable',message='账号列表读取失败，请重新检查登录状态。')

    def _load(self):
        book = self._book()
        value = {} if self.adding else book['accounts'].get(self.target, {})
        return {**value, 'host':book['host']} if book['host'] else dict(value)

    def _write(self, value, *, guarded):
        book = self._book()
        if (value.get('client_id') and value.get('subject') and
                (self.adding or book['active'] is None) and registration_id(value) != book['active']):
            return self.account_change(lambda: self._write_direct(value, guarded=guarded), ready=self._ready(value))
        return self._write_direct(value, guarded=guarded)

    def _ready(self, value):
        session = value.get('session', {})
        return (bool(session.get('access_token')) and
                'chatgpt.tokens.use.direct' in session.get('scopes', []) and
                session.get('expires_at', 0) > self.clock() + 120 and
                value.get('renewal', {}).get('status') in (None, 'complete', 'temporary_failure'))

    def ready_for_model(self):
        return self._ready(selected(self._book()))

    def model_access(self):
        """Trusted executor only; never expose this through management operations."""
        from .desktop_model import ModelAccess

        book = self._book()
        value = selected(book)
        if not self._ready(value):
            raise AssistantError('model_auth', '所选账号授权尚未就绪，请检查登录状态。')
        return ModelAccess(book['active'], value['session']['access_token'])

    def check_model_access(self, access):
        book = self._book()
        value = selected(book)
        session = value.get('session', {})
        if (book['active'] != access.account_id or session.get('access_token') != access.access_token
                or session.get('expires_at', 0) <= self.clock()
                or 'chatgpt.tokens.use.direct' not in session.get('scopes', [])
                or value.get('renewal', {}).get('status') not in (None, 'complete', 'temporary_failure')):
            raise AssistantError('model_auth', '本次模型授权已失效，任务已停止，请检查当前账号。')

    def _write_direct(self, value, *, guarded):
        with self.lock, self.store.lock, self.store.db() as db:
            if guarded:
                self._guard()
            book = catalog(self.store.state(KEY, db=db) or {})
            book['host'] = value.get('host', book['host'])
            if value.get('client_id') and value.get('subject'):
                identifier = registration_id(value)
                if not self.adding and self.target and identifier != self.target:
                    raise ValueError('Selected registration identity changed')
                existing = book['accounts'].get(identifier)
                number = existing['number'] if existing else max((a['number'] for a in book['accounts'].values()),default=0)+1
                book['accounts'][identifier] = {**value, 'number':number}
                if self.adding or book['active'] is None:
                    book['active'] = identifier
                    self.target = identifier
            book['revision'] += 1
            self.store.set_state(KEY, book, db)

    def _save(self, value):
        self._write(value, guarded=True)

    def _retain_rotation(self, value):
        self._write(value, guarded=False)

    def _operate_locked(self, operation):
        book = self._book()
        self.target = book['active']
        self.adding = operation == 'add'
        if operation == 'select':
            identifier, expected = self.selection
            if book['revision'] != expected or identifier not in book['accounts']:
                raise AssistantError('model_account_changed','账号列表已更新，请重新选择')
            def commit():
                with self.lock, self.store.lock, self.store.db() as db:
                    self._guard()
                    current = catalog(self.store.state(KEY, db=db) or {})
                    if current['revision'] != expected:
                        raise AssistantError('model_account_changed','账号列表已更新，请重新选择')
                    current['active'] = identifier
                    current['revision'] += 1
                    self.store.set_state(KEY, current, db)
            self.account_change(commit, ready=self._ready(book['accounts'][identifier]))
            self.target = identifier
            self._public(book['accounts'][identifier])  # Runtime owns task takeover; selection does not renew.
            return
        super()._operate_locked('login' if operation == 'add' else operation)
