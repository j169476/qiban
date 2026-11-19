"""Application inbox for fixed diagnostic messages, never sent to a Codex chat."""
import secrets
from datetime import datetime, timedelta, timezone

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
    'credential_refresh_required':'B 站要求更新登录。请在「平台与会话 → B 站」用手机扫码重新登录，再启动 B 站。',
    'credential_refresh_uncertain':'B 站未能确认登录续期结果。请重新扫码登录，再启动 B 站。',
    'credential_check_failed':'B 站暂时未能核验登录状态，请稍后重试。',
    'qq_login_required':'QQ 登录已失效，请在平台与会话中重新登录后启动。',
    'qq_login_timeout':'QQ 登录连接超时，请检查网络后重新启动。',
    'qq_native_disconnected':'QQ 后台连接已断开，请检查登录状态后重新启动。',
    'qq_native_not_ready':'QQ 会话尚未就绪，请检查登录状态后重新启动。',
    'model_account_switching':'账号交接尚未完成，所有任务保持停止。请在模型账号页面重新选择账号。',
    'model_unavailable':'模型尚未就绪，平台保持停止。请检查模型登录与运行状态。',
    'platform_unavailable':'平台连接器尚未接入，未启动活动。',
    'client_version_unsupported':'当前电脑版微信与读取组件不兼容，个人号自动回复已暂停。请在微信账号页检测版本。',
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
            now=utcnow()
            bucket='desktop-incident-day:'+now[:10]+':'
            previous=self.store.state(marker,db=db)
            if previous:
                return previous['id']
            counts = self._counts(db)
            group='desktop-fault-group:'+stable_key('fault',platform,code)
            latest=self.store.state(group,{},db)
            old_key='desktop-notice:'+latest.get('id','')
            old=self.store.state(old_key,db=db)
            identifier=now+':'+secrets.token_hex(8)
            if old:
                db.execute('DELETE FROM state WHERE key=?',(old_key,))
                counts['total'] -= 1
                counts['unread'] -= int(not old['read'])
            self.store.set_state('desktop-notice:'+identifier,{'id':identifier,'created_at':now,'code':code,
                'platform':platform,'message':MESSAGES[code],'read':False,
                'occurrences':(old or {}).get('occurrences',int(bool(old)))+1},db)
            self.store.set_state(group,{'id':identifier},db)
            self.store.set_state(marker,{'id':identifier},db)
            self.store.set_state(bucket+marker,True,db)
            self.store.set_state(COUNTS_KEY, {'total': counts['total'] + 1, 'unread': counts['unread'] + 1}, db)
        return identifier

    def cleanup(self, *, now=None, limit=200):
        cutoff=((now or datetime.now(timezone.utc))-timedelta(days=90)).isoformat()
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            counts=self._counts(db)
            prefix='desktop-incident-day:'
            for row in db.execute("SELECT key FROM state WHERE key LIKE 'desktop-incident-day:%' "
                                  "AND key<? ORDER BY key LIMIT ?",(prefix+cutoff[:10],limit)):
                marker=row[0][len(prefix)+11:]
                db.execute('DELETE FROM state WHERE key IN (?,?)',(row[0],marker))
            # Visit old state keys in bounded pages, including installations with undated markers.
            cursor_key='desktop-inbox-cleanup-cursor'
            after=self.store.state(cursor_key,'',db)
            rows=db.execute("SELECT key,value FROM state WHERE key>? AND "
                            "(key LIKE 'desktop-notice:%' OR key LIKE 'desktop-notice-incident:%') "
                            "ORDER BY key LIMIT ?",(after,limit)).fetchall()
            totals_before=counts.copy()
            for key,value in rows:
                item=self.store.ledger_decode('state:'+key,value)
                if key.startswith('desktop-notice:') and item.get('read') and not item.get('read_at'):
                    item={**item,'read_at':(now or datetime.now(timezone.utc)).isoformat()}
                    self.store.set_state(key,item,db)
                if key.startswith('desktop-notice-incident:'):
                    stamp=item['id'][:10]
                    if len(stamp)==10 and stamp<cutoff[:10]:
                        db.execute('DELETE FROM state WHERE key=?',(key,))
                elif item.get('read') and item['read_at']<cutoff:
                    db.execute('DELETE FROM state WHERE key=?',(key,))
                    counts['total']-=1
                elif 'occurrences' not in item and item.get('code') in MESSAGES and item.get('platform'):
                    item=self.store.state(key,db=db)
                    if item is None or 'occurrences' in item:
                        continue
                    group='desktop-fault-group:'+stable_key('fault',item['platform'],item['code'])
                    pointer=self.store.state(group,{},db)
                    other=self.store.state('desktop-notice:'+pointer.get('id',''),db=db)
                    if other and other['id']!=item['id']:
                        latest,earlier=sorted((item,other),key=lambda notice:notice['id'],reverse=True)
                        latest={**latest,'read':item['read'] and other['read'],
                                'occurrences':other.get('occurrences',1)+1}
                        counts['total']-=1
                        counts['unread']-=int(not item['read'] and not other['read'])
                        db.execute('DELETE FROM state WHERE key=?',('desktop-notice:'+earlier['id'],))
                    else:
                        latest={**item,'occurrences':1}
                    self.store.set_state('desktop-notice:'+latest['id'],latest,db)
                    self.store.set_state(group,{'id':latest['id']},db)
            if counts!=totals_before:
                self.store.set_state(COUNTS_KEY,counts,db)
            next_key=rows[-1][0] if len(rows)==limit else ''
            if next_key!=after:
                self.store.set_state(cursor_key,next_key,db)

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
                    self.store.set_state(key, {**item, 'read': True, 'read_at':utcnow()}, db)
                counts['unread'] -= int(not item['read'])
                changed += 1
            if changed:
                self.store.set_state(COUNTS_KEY, counts, db)
        return {'changed': changed}

    def mark_read(self, request, identifier):
        self.update(request, [identifier], 'read', require_existing=True)
        return {'read': True, 'id': identifier}
