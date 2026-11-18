"""Application inbox for fixed diagnostic messages, never sent to a Codex chat."""
import secrets

from companion_codex.failures import MESSAGES as MODEL_MESSAGES
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.storage import utcnow
from companion_qq.native_process import READ_FAILURE_MESSAGES

MESSAGES = {
    **MODEL_MESSAGES,
    **READ_FAILURE_MESSAGES,
    'qq_native_read_failed':'QQ 读取消息失败，请查看本次运行记录。',
    'scheduler_failed':'后台任务调度已停止，请检查诊断并重启应用后台。',
    'inbox_timeout':'收信连续超时，该平台已暂停；请检查连接后重新启动。',
    'network_error':'平台连接连续失败，该平台已暂停；请检查网络后重新启动。',
    'read_timeout':'平台读取连续超时，已暂停；请稍后重新启动。',
    'platform_read_error':'平台数据连续读取失败，已暂停；请稍后重新启动。',
    'login_required':'平台登录已失效，请重新登录后启动。',
    'qq_login_required':'QQ 登录已失效，请在平台与会话中重新登录后启动。',
    'qq_login_timeout':'QQ 登录连接超时，请检查网络后重新启动。',
    'qq_native_disconnected':'QQ 后台连接已断开，请检查登录状态后重新启动。',
    'qq_native_not_ready':'QQ 会话尚未就绪，请检查登录状态后重新启动。',
    'model_account_switching':'账号交接尚未完成，所有任务保持停止。请在模型账号页面重新选择账号。',
    'model_unavailable':'模型尚未就绪，平台保持停止。请检查模型登录与运行状态。',
    'platform_unavailable':'平台连接器尚未接入，未启动活动。',
    'account_changed':'自动运行时发现账号已改变，请核对账号并重新设置自动运行。',
    'runtime_restarted':'后台曾中断，旧运行已停止；未发出的动作已取消，结果不明的发送不会自动补发。',
    'runtime_failed':'平台运行发生错误并已停止，请检查运行记录。',
}


PAGE_SIZE = 10
COUNTS_KEY = 'desktop-inbox-counts'


class DesktopNotifications:
    def __init__(self, management):
        self.management,self.store = management,management.store

    def record(self, code, incident, platform):
        """Trusted runtime only; no caller-supplied free text enters diagnostic messages."""
        if code not in MESSAGES:
            code='runtime_failed'
        marker='desktop-notice-incident:'+stable_key('incident',incident,platform,code)
        with self.store.lock,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=self.store.state(marker,db=db)
            if previous:
                return previous['id']
            counts = self._counts(db)
            now=utcnow()
            identifier=now+':'+secrets.token_hex(8)
            self.store.set_state('desktop-notice:'+identifier,{'id':identifier,'created_at':now,'code':code,
                'platform':platform,'message':MESSAGES[code],'read':False},db)
            self.store.set_state(marker,{'id':identifier},db)
            self.store.set_state(COUNTS_KEY, {'total': counts['total'] + 1, 'unread': counts['unread'] + 1}, db)
        return identifier

    def _counts(self, db):
        counts = self.store.state(COUNTS_KEY, db=db)
        if counts is None:
            # Older installations have encrypted notice bodies and no counters.
            counts = {'total': 0, 'unread': 0}
            for row in db.execute("SELECT key,value FROM state WHERE key LIKE 'desktop-notice:%'"):
                item = self.store.ledger_decode('state:' + row['key'], row['value'])
                counts['total'] += 1
                counts['unread'] += int(not item['read'])
            self.store.set_state(COUNTS_KEY, counts, db)
        return counts

    def _items(self, db, limit, offset=0):
        rows = db.execute("SELECT key,value FROM state WHERE key LIKE 'desktop-notice:%' "
                          "ORDER BY key DESC LIMIT ? OFFSET ?", (limit, offset))
        return [self.store.ledger_decode('state:' + row['key'], row['value']) for row in rows]

    def listing(self, request):
        self.management.authority.require(request, administrator=True)
        with self.store.db() as db:
            items = self._items(db, 200)
        return {'items': items, 'unread': sum(not item['read'] for item in items), 'limit': 200}

    def page(self, request, page):
        self.management.authority.require(request, administrator=True)
        if type(page) is not int or page < 1:
            raise AssistantError('notification_request', '页码无效')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            counts = self._counts(db)
            pages = max(1, (counts['total'] + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, pages)
            items = self._items(db, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        return {**counts, 'page': page, 'pages': pages, 'items': items}

    def update(self, request, identifiers, action, *, require_existing=False):
        self.management.authority.require(request, administrator=True)
        if (action not in ('read', 'delete') or not isinstance(identifiers, list)
                or not 1 <= len(identifiers) <= PAGE_SIZE
                or any(not isinstance(i, str) or not 1 <= len(i) <= 100 for i in identifiers)):
            raise AssistantError('notification_request', '通知选择无效')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.management.authority.require(request, administrator=True)
            counts = self._counts(db)
            changed = 0
            for identifier in dict.fromkeys(identifiers):
                key = 'desktop-notice:' + identifier
                item = self.store.state(key, db=db)
                if item is None:
                    if require_existing:
                        raise AssistantError('not_found', '通知不存在')
                    continue
                if action == 'delete':
                    db.execute('DELETE FROM state WHERE key=?', (key,))
                    counts['total'] -= 1
                elif item['read']:
                    continue
                else:
                    self.store.set_state(key, {**item, 'read': True}, db)
                counts['unread'] -= int(not item['read'])
                changed += 1
            if changed:
                self.store.set_state(COUNTS_KEY, counts, db)
        return {'changed': changed}

    def mark_read(self, request, identifier):
        self.update(request, [identifier], 'read', require_existing=True)
        return {'read': True, 'id': identifier}
