"""Local backup previews and native-Windows-authorized restore transactions."""
import hashlib
import hmac
import secrets

from companion_core import asset_transfer, postgres_transfer
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.runtime_keys import RUN_KEYS
from companion_core.storage import utcnow

from . import desktop_identity

RESTORE = 'desktop-last-restore'


def finish_pending(store):
    """Resume only a committed, explicitly authorized desktop restore."""
    receipt = store.state(RESTORE, {})
    if receipt.get('status') == 'assets_pending':
        asset_transfer.finish(store)
        with store.lock, store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            store.set_state(RESTORE, {**receipt, 'status':'complete'}, db)
    return store.state(RESTORE)


class DesktopBackups:
    def __init__(self, management):
        self.management, self.store = management, management.store
        self.preview = self.pending = None

    def require(self):
        request = self.management.request()
        self.management.authority.require(request, administrator=True)
        return request

    def path(self, name):
        if (not isinstance(name,str) or not name or '/' in name or '\\' in name
                or ':' in name or not name.endswith('.ccbackup')):
            raise AssistantError('backup_path','备份名称无效')
        folder = self.store.root/'history'/'backups'
        path = folder/name
        if not path.is_file() or path.is_symlink() or path.resolve().parent != folder.resolve():
            raise AssistantError('backup_path','备份不存在')
        if path.stat().st_size > postgres_transfer.MAX_BACKUP:
            raise AssistantError('backup_limit','备份超过当前恢复上限')
        return path

    def listing(self):
        self.require()
        folder = self.store.root/'history'/'backups'
        return {'backups':[{'name':p.name,'bytes':p.stat().st_size} for p in sorted(folder.glob('*.ccbackup'),reverse=True)
                           if p.is_file() and not p.is_symlink() and p.resolve().parent==folder.resolve()],
                'last_restore':self.store.state(RESTORE)}

    def create(self):
        request = self.require()
        if self.store.state(RESTORE,{}).get('status')=='assets_pending':
            raise AssistantError('restore_pending','请先完成上次资料恢复')
        result = self.store.backup(force=True)
        name = result['path'].replace('\\','/').rsplit('/',1)[-1]
        self.management.repository.audit(request.actor,'desktop_backup_created',name)
        return {'name':name,'sha256':result['sha256'],'encrypted':True}

    def _paused(self):
        runtime = self.management.runtime
        if runtime is not None and getattr(runtime, 'manual', None) is not None and runtime.manual.engine.busy:
            raise AssistantError('restore_stopping', '手动任务仍在退出，请稍后再恢复资料。')
        if runtime is not None and runtime.scheduler is not None and any(
                runtime.scheduler.status(p)['busy'] or runtime.scheduler.status(p)['stopping']
                for p in runtime.cores):
            raise AssistantError('restore_stopping', '后台任务仍在退出，请等待停止完成后再恢复资料。')
        if any(self.store.state(key,{}).get('status')=='active' for key in RUN_KEYS):
            raise AssistantError('restore_active','请先停止角色和平台，再查看恢复预览')
        if self.store.state(RESTORE,{}).get('status')=='assets_pending':
            raise AssistantError('restore_pending','请先完成上次资料恢复')

    def prepare(self, name):
        self.preview = self.pending = None
        request = self.require()
        with self.store.lock:
            self._paused()
            raw = self.path(name).read_bytes()
            snapshot = postgres_transfer.unpack(raw,self.store._backup_key)
            token = secrets.token_urlsafe(32)
            self.preview = {'token':token,'name':name,'hash':hashlib.sha256(raw).hexdigest(),
                            'expected':postgres_transfer.fingerprint(self.store),'session':request.session_id,
                            'expires':self.management.clock()+180}
        return {'token':token,'name':name,'expires_in_seconds':180,
                'records':sum(len(snapshot['tables'][table]['rows']) for table in ('content_records','content_history')),
                'effect':'将覆盖当前资料和历史记录；保留当前本机管理员与应用密码锁。角色保持停止，平台授权需重新检查。'}

    def begin(self, token):
        self.pending = None
        preview,self.preview = self.preview,None
        request = self.require()
        if (not preview or not isinstance(token,str) or not hmac.compare_digest(preview['token'],token)
                or preview['session']!=request.session_id or preview['expires']<=self.management.clock()):
            raise AssistantError('restore_preview_expired','恢复预览已失效，请重新查看')
        self._paused()
        challenge = secrets.token_urlsafe(32)
        self.pending = {**preview,'challenge':challenge,'expires':self.management.clock()+180}
        return {'challenge':challenge}

    def complete(self, challenge):
        pending,self.pending = self.pending,None
        request = self.require()
        if (not pending or not isinstance(challenge,str) or not hmac.compare_digest(pending['challenge'],challenge)
                or pending['session']!=request.session_id or pending['expires']<=self.management.clock()):
            raise AssistantError('restore_verification_expired','恢复验证已失效，请重新预览并验证 Windows 身份')
        with self.store.lock:
            self._paused()
            raw = self.path(pending['name']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=pending['hash']:
                raise AssistantError('edit_conflict','备份文件在预览后已改变，请重新检查')
            owner,lock = self.management._owner(),self.management._lock()
            lock = {**lock,'revision':lock['revision']+1}
            # This installation's logical identities survive; a snapshot may carry another one.
            role,identity = self.store.state(desktop_identity.ROLE),desktop_identity.binding(self.store)
            receipt = {'id':secrets.token_hex(16),'name':pending['name'],'status':'assets_pending',
                       'completed_at':utcnow(),'role_started':False}
            def finalize(db):
                # A backup must never roll back the current installation identity or lock.
                self.store.set_state('desktop-owner',owner,db)
                self.store.set_state('desktop-app-lock',lock,db)
                if self.management.runtime is not None:
                    self.management.runtime.preserve_after_restore(db)
                desktop_identity.restore_local(self.store,role=role,existing=identity,db=db)
                db.execute("DELETE FROM state WHERE key LIKE 'desktop-request:%'")
                self.store.set_state(RESTORE,receipt,db)
                GovernanceRepository(self.store).audit(request.actor,'desktop_restore_verified_windows',pending['name'],db=db)
            try:
                result = postgres_transfer.restore(self.store,raw,self.store._backup_key,
                                                   expected=pending['expected'],finalize=finalize)
            finally:
                # The database may have committed even if restoring a registered file failed.
                if self.store.state(RESTORE,{}).get('id')==receipt['id']:
                    self.management.revision = lock['revision']
                    self.management.records.delete_previews.clear()
            finish_pending(self.store)
        return {**result,'last_restore':self.store.state(RESTORE)}

    def dispatch(self, message):
        """Private native-host channel; page/model operations cannot assert verification."""
        try:
            if (not isinstance(message,dict) or set(message)!={'action','operation','arguments'}
                    or message['action']!='desktop-restore' or not isinstance(message['arguments'],dict)):
                raise AssistantError('management_request','恢复请求格式无效')
            op,args = message['operation'],message['arguments']
            if op=='begin' and set(args)=={'token'}:
                result = self.begin(args['token'])
            elif op=='complete' and set(args)=={'challenge'}:
                result = self.complete(args['challenge'])
            elif op=='cancel' and not args:
                self.pending = self.preview = None
                result = {'cancelled':True}
            else:
                raise AssistantError('management_request','恢复请求格式无效')
            return {'ok':True,'result':result}
        except AssistantError as error:
            return {'ok':False,'error':error.code,'message':str(error)}
