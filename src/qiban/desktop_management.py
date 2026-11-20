"""Local desktop administration, independently of Codex and browser sessions."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid

from companion_core.access import RequestAuthority
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import AdminService, GovernanceRepository
from companion_core.record_management import RecordManagement
from companion_core.sessions import password_hash

from .desktop_backups import DesktopBackups
from .desktop_identity import OWNER
from .desktop_notifications import DesktopNotifications

LOCK = 'desktop-app-lock'


def provision_owner(store, installation, windows_sid):
    """Trusted installer only. Missing runtime identity is never auto-repaired."""
    from .desktop_runtime import provision_runtime

    with store.lock, store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = store.state(OWNER, db=db)
        if existing:
            if existing['installation'] != installation or existing['windows_sid'] != windows_sid:
                raise AssistantError('desktop_owner_mismatch', '当前 Windows 用户与本次安装不匹配')
            provision_runtime(store,installation,db)
            return existing
        owner = {'format': 1, 'installation': installation, 'windows_sid': windows_sid,
                 'actor': ActorRef('local', uuid.uuid4().hex, installation).as_dict()}
        store.set_state(OWNER, owner, db)
        store.set_state(LOCK, {'enabled': False, 'revision': 0, 'attempts': 0, 'until': 0}, db)
        provision_runtime(store,installation,db)
        GovernanceRepository(store).audit(ActorRef(**owner['actor']).key, 'desktop_owner_created', installation, db=db)
        return owner


class DesktopManagement:
    """Construct only after transport/OS identity verification; never from model arguments."""
    def __init__(self, store, installation, windows_sid, *, clock=time.time):
        self.store, self.installation, self.windows_sid, self.clock = store, installation, windows_sid, clock
        self.owner = self._owner()
        self.actor = ActorRef(**self.owner['actor'])
        self.instance = uuid.uuid4().hex
        self._recovery = None
        state = self._lock()
        self.unlocked = not state['enabled']
        self.revision = state['revision']
        self.repository = GovernanceRepository(store, clock=clock)
        self.authority = RequestAuthority(epoch=self.repository.epoch, session_active=self._active, clock=clock)
        self.admin = AdminService(store, repository=self.repository, authority=self.authority)
        self.records = RecordManagement(store, self.admin)
        self.backups = DesktopBackups(self)
        self.runtime = None
        self.codex = None
        self.qq = None
        self.bili = None
        from .desktop_learning_index import LearningIndex
        self.learning_index = LearningIndex(store)
        self.wechat_resources = None
        self.wechat_detected = {}
        from .desktop_room import DesktopRoom
        self.room = DesktopRoom(self)
        from .desktop_audiences import DesktopAudiences
        self.audiences = DesktopAudiences(self)
        self.notifications = DesktopNotifications(self)
        from .desktop_growth import DesktopGrowth
        self.growth = DesktopGrowth(self)
        # Sharing previews are held per management instance, so a restart or a lock
        # can never confirm a scope the administrator saw before. The owner's own
        # sealed identity is passed so an effect check can use it when it really
        # owns the record, and the desktop conversation is bound so no other
        # sealed administrator request can drive this page.
        from companion_core.record_sharing import RecordSharing
        self.sharing = RecordSharing(self.admin, actor=self.actor,
                                     conversation='desktop:' + self.instance)
        # Interpersonal claim adjudication keeps its own preview set for the same
        # reason: a restart or a lock can never confirm a status the administrator
        # saw before, and the desktop conversation is bound exactly as above.
        from companion_core.relation_claims import RelationClaims
        self.relations = RelationClaims(self.admin, actor=self.actor,
                                        conversation='desktop:' + self.instance)

    def _owner(self):
        owner = self.store.state(OWNER)
        if not owner:
            raise AssistantError('desktop_owner_missing', '本机管理员尚未建立，请检查安装状态')
        if owner['installation'] != self.installation or owner['windows_sid'] != self.windows_sid:
            raise AssistantError('desktop_owner_mismatch', '当前 Windows 用户与本次安装不匹配')
        return owner

    def _lock(self):
        value = self.store.state(LOCK)
        if not value or type(value.get('enabled')) is not bool or type(value.get('revision')) is not int:
            raise AssistantError('desktop_lock_invalid', '应用锁状态无效，未授予管理权限')
        return value

    def _active(self, session):
        self._owner()
        state = self._lock()
        return session == self.session_id and self.unlocked and self.revision == state['revision']

    @property
    def session_id(self):
        return f'{self.instance}:{self.revision}'

    def status(self):
        state = self._lock()
        unlocked = self._active(self.session_id)
        return {'windows_user_verified': True, 'lock_enabled': state['enabled'],
                'locked': not unlocked, 'lock_revision': state['revision'],
                'room_session': self.session_id if unlocked else None}

    def request(self):
        if not self._active(self.session_id):
            raise AssistantError('desktop_locked', '请先解锁栖伴管理功能')
        return self.authority.issue(actor=self.actor, conversation='desktop:'+self.instance, audience='private',
            recipients=(self.actor,), purpose='management', administrator=True, session_id=self.session_id, lifetime=60)

    def room_actor(self):
        """Private-room logical identity; management rights stay with the current owner."""
        from .desktop_identity import resolve

        return resolve(self.store, installation=self.installation, owner=self.actor) or self.actor

    def _verify_password(self, password):
        denied = False
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._lock()
            if not state['enabled']:
                raise AssistantError('desktop_lock_disabled', '尚未启用应用密码锁')
            now = self.clock()
            attempts = state['attempts'] if state['until'] > now else 0
            if attempts >= 5:
                raise AssistantError('desktop_unlock_limited', '尝试次数过多，请稍后再试')
            try:
                valid = hmac.compare_digest(password_hash(password,state['salt']),state['hash'])
            except AssistantError:
                valid = False
            if valid:
                state.update(attempts=0,until=0)
            else:
                state.update(attempts=attempts+1,until=state['until'] if state['until'] > now else now+300)
                self.repository.audit(self.actor.key,'desktop_unlock_denied',self.installation,db=db)
                denied = True
            self.store.set_state(LOCK,state,db)
        if denied:
            raise AssistantError('desktop_unlock_denied','密码不正确')
        return state

    def unlock(self, password):
        state = self._verify_password(password)
        self.revision, self.unlocked = state['revision'], True
        self.repository.audit(self.actor.key,'desktop_unlocked',self.installation)
        return self.status()

    def lock(self):
        self.authority.require(self.request(),administrator=True)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._lock()
            if not state['enabled']:
                raise AssistantError('desktop_lock_disabled','先启用应用密码锁，才能锁定管理功能')
            state['revision'] += 1
            self.store.set_state(LOCK,state,db)
            self.repository.audit(self.actor.key,'desktop_locked',self.installation,db=db)
        self.unlocked = False
        self.learning_index.invalidate()
        if self.runtime:
            self.runtime.manual.stop('desktop_locked')
        if self.bili:
            self.bili.close()
        return self.status()

    def enable_lock(self, password):
        request = self.request()
        salt = secrets.token_hex(16)
        hashed = password_hash(password,salt)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.authority.require(request,administrator=True)
            state = self._lock()
            if state['enabled']:
                raise AssistantError('desktop_lock_enabled','应用锁已开启，请先验证密码关闭后再设置')
            state = {'enabled':True,'revision':state['revision']+1,'salt':salt,'hash':hashed,'attempts':0,'until':0}
            self.store.set_state(LOCK,state,db)
            self.repository.audit(self.actor.key,'desktop_lock_enabled',self.installation,db=db)
        self.revision = state['revision']
        return self.status()

    def disable_lock(self, password):
        self.authority.require(self.request(),administrator=True)
        verified = self._verify_password(password)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._lock()
            if state['revision'] != verified['revision']:
                raise AssistantError('edit_conflict','应用锁状态已改变，请重新操作')
            state = {'enabled':False,'revision':state['revision']+1,'attempts':0,'until':0}
            self.store.set_state(LOCK,state,db)
            self.repository.audit(self.actor.key,'desktop_lock_disabled',self.installation,db=db)
        self.revision, self.unlocked = state['revision'], True
        return self.status()

    def begin_recovery(self, password):
        """Private native-host route; never registered as a page/model operation."""
        self._recovery = None
        self._owner()
        state = self._lock()
        if not state['enabled']:
            raise AssistantError('desktop_lock_disabled', '尚未启用应用密码锁')
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        challenge = secrets.token_urlsafe(32)
        self._recovery = {'challenge':challenge, 'revision':state['revision'],
                          'expires':self.clock()+180, 'salt':salt, 'hash':hashed}
        return {'challenge':challenge}

    def finish_recovery(self, challenge):
        """Call only after Windows verified the current native host's user."""
        pending, self._recovery = self._recovery, None
        if (not pending or not isinstance(challenge,str)
                or not hmac.compare_digest(pending['challenge'],challenge) or pending['expires']<=self.clock()):
            raise AssistantError('desktop_recovery_expired','重设验证已失效，请重新验证 Windows 身份')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owner()
            state = self._lock()
            if not state['enabled'] or state['revision']!=pending['revision']:
                raise AssistantError('edit_conflict','应用锁状态已改变，请重新验证 Windows 身份')
            state = {'enabled':True,'revision':state['revision']+1, 'salt':pending['salt'],
                     'hash':pending['hash'],'attempts':0,'until':0}
            self.store.set_state(LOCK,state,db)
            self.repository.audit(self.actor.key,'desktop_lock_recovered_windows',self.installation,db=db)
        self.revision, self.unlocked = state['revision'], True
        return self.status()

    def recovery_dispatch(self, message):
        """Only the authenticated native pipe peer may reach this separate route."""
        try:
            if not isinstance(message,dict) or set(message)!={'action','operation','arguments'} or message['action']!='desktop-recovery':
                raise AssistantError('management_request','重设请求格式无效')
            op,args = message['operation'],message['arguments']
            if op=='begin' and isinstance(args,dict) and set(args)=={'password'}:
                result=self.begin_recovery(args['password'])
            elif op=='complete' and isinstance(args,dict) and set(args)=={'challenge'}:
                result=self.finish_recovery(args['challenge'])
            elif op=='cancel' and args=={}:
                self._recovery=None
                result={'cancelled':True}
            else:
                raise AssistantError('management_request','重设请求格式无效')
            return {'ok':True,'result':result}
        except AssistantError as error:
            self.repository.audit(self.actor.key,'desktop_recovery_denied',error.code)
            return {'ok':False,'error':error.code,'message':str(error)}

    def dispatch(self, message):
        """One strict operation registry; no caller-supplied actor, session or admin flag."""
        try:
            if (not isinstance(message,dict) or set(message)!={'action','operation','arguments','request_id'}
                    or message['action']!='management' or not isinstance(message['arguments'],dict)
                    or not isinstance(message['request_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',message['request_id'])):
                raise AssistantError('management_request','管理请求格式无效')
            op,args = message['operation'],message['arguments']
            fields = {'session.status':set(),'session.lock':set(),'session.unlock':{'password'},
                      'lock.enable':{'password'},'lock.disable':{'password'},'config.read':{'kind'},
                      'config.patch':{'kind','changes','expected'},
                      'records.list':{'kind','after','limit'},
                      'records.read':{'kind','identifier'},
                      'records.explain':{'kind','identifier'},
                      'records.correct':{'kind','identifier','value','expected'},
                      'records.export':{'kind','identifier'},
                      'records.import_preview':{'kind','identifier','text','expected_version'},
                      'records.advanced_export':{'kind','identifier'},
                      'records.advanced_preview':{'kind','identifier','text','expected_version'},
                      'records.advanced_commit':{'token'},'records.advanced_cancel':{'token'},
                      'records.advanced_changes':{'token','offset'},
                      'records.delete_preview':{'kind','identifier'},
                      'records.delete':{'token'},
                      'sharing.records':{'after','limit'},'sharing.record':{'kind','identifier'},
                      'sharing.targets':{'after'},'sharing.grants':{'kind','identifier','after'},
                      'sharing.preview_grant':{'kind','identifier','grantee','owner','purposes','days'},
                      'sharing.preview_classify':{'kind','identifier','classification'},
                      'sharing.preview_revoke':{'kind','identifier','grant_id'},
                      'sharing.commit':{'token'},'sharing.cancel':{'token'},
                      'relations.list':{'after','limit','status'},
                      'relations.detail':{'identifier'},
                      'relations.history':{'identifier','after','limit'},
                      'relations.preview':{'identifier','status','reason'},
                      'relations.commit':{'token'},'relations.cancel':{'token'},
                      'growth.preview':set(),'growth.rebuild':{'token'},
                      'growth.archives':{'after'},'growth.archive':{'identifier'},
                      'facts.retention':set(),'facts.retention_save':{'days','expected'},
                      'facts.history':{'identifier','after','at'},
                      'facts.confirm':{'identifier','expected','reason','supersedes'},
                      'facts.correct':{'identifier','expected','reason','supersedes','value'},
                      'foundation.add':{'identifier','content','expected'},'actors.list':set(),
                      'relationship.link_preview':{'left','right'},'relationship.link_confirm':{'left','right','expected'},
                      'relationship.links':{'after'},'relationship.link_detail':{'identifier'},
                      'relationship.link_revoke':{'identifier','expected','reason'},
                      'audience.status':{'platform'},'audience.preview':{'platform','kind','enabled','account'},'audience.save':{'token'},
                      'reply_quota.summary':{'after'},'reply_quota.scopes':{'platform','account','after'},
                      'reply_quota.account_save':{'platform','account','changes','expected'},
                      'reply_quota.scope_save':{'scope_id','account','expected','changes'},
                      'reply_quota.scope_reset':{'scope_id','account','expected'},
                      'reply_exclusion.exclude':{'scope_id','account','expected'},
                      'reply_exclusion.restore':{'scope_id','account','expected'},
                      'reply_trace.list':{'limit'},'reply_trace.detail':{'identifier'},
                      'wechat.accounts':set(),'wechat.profiles':set(),'wechat.select':{'login','expected'},
                      'wechat.account_details':{'login'},
                      'room.history':{'session','before'},'room.post':{'session','text','nonce'},
                      'room.post_files':{'session','text','nonce','files'},'room.preview':{'session','turn','image'},
                      'room.queue':{'session'},'room.search':{'session','before','query'},
                      'room.cancel':{'session','id'},
                      'tasks.list':{'session'},'tasks.submit':{'session','kind','arguments','nonce'},
                      'tasks.detail':{'session','identifier','offset'},
                      'tasks.control':{'session','identifier','action','expected'},'runtime.stop_all':set(),
                      'wechat.directory':{'login','expected','kind','after'},
                      'wechat.directory_page':{'login','expected','kind','query','page'},
                      'wechat.directory_sync':{'login','expected'},
                      'wechat.targets.verify':{'login','expected','targets'},
                      'wechat.targets.saved':{'login','expected'},'wechat.targets.save':{'login','expected','targets','version'},
                      'relationship.clear':{'expected'},'relationship.read':set(),'relationship.accounts':{'after'},'relationship.person':{'anchor'},
                      'relationship.save':{'anchor','selected','expected','setting'},'baseline.preview':set(),'baseline.publish':{'expected'},
                      'backups.list':set(),'backups.create':set(),'backups.preview':{'name'},
                      'runtime.status':set(),'runtime.preference':{'platform','enabled','expected'},
                      'runtime.control':{'platform','action'},'notifications.list':set(),'notifications.read':{'identifier'},
                      'notifications.page':{'page'},'notifications.update':{'identifiers','action'},
                      'model.status':set(),'model.refresh':set(),'model.login':set(),'model.logout':set(),
                      'model.cancel':set(),'model.open':set(),'model.add':set(),
                      'model.select':{'account_id','expected'},
                      'model.catalog':{'refresh'},'model.configure':{'account_id','expected','model','effort'},
                      'qq.password':{'account','password'},'qq.status':set(),'qq.refresh':set(),'qq.login':set(),'qq.cancel':set(),
                      'bili.browser':{'session'},'bili.browser_complete':{'session','ticket','cookies'},
                      'bili.status':{'session'},'bili.login':{'session'},'bili.check':{'session'},
                      'bili.confirm':{'session','ticket'},'bili.cancel':{'session','ticket'},
                      'bili.policy':{'session','uid'},'bili.policy_save':{'session','uid','expected','actions'},
                      'qq.select':{'account','expected'},
                      'qq.targets.verify':{'private_targets','groups','expected'},
                      'qq.targets.save':{'token','expected'},'qq.directory':{'expected'},
                      'files.settings':set(),'files.settings.configure':{'count','total_mib','expected'},
                      'files.list':{'after','limit'},'files.targets':set(),'files.import_file':{'path','tags'},
                      'files.configure':{'identifier','name','tags','enabled','allowed_scopes','expected'},
                      'stickers.list':{'after','limit'},'stickers.import_file':{'path','name','tags'},
                      'stickers.configure':{'identifier','name','tags','enabled','expected'},
                      'media.settings':set(),'media.configure':{'days','expected'},
                      'media.budget':set(),'media.budget.configure':{'limits','expected'},
                      'media.voice':set(),'media.voice.configure':{'enabled','seconds','expected'},
                      'media.capacity':set(),'media.capacity.configure':{'mib','expected'},
                      'media.list':{'after','limit'},'media.preserve':{'identifier','reasons','expected'}}
            from . import (
                desktop_learning,
                desktop_library_versions,
                desktop_record_versions,
                desktop_workspace,
            )
            fields.update(desktop_workspace.FIELDS)
            fields.update(desktop_learning.FIELDS)
            fields.update(desktop_record_versions.FIELDS)
            fields.update(desktop_library_versions.FIELDS)
            if op == 'model.configure' and 'ultrafast' in args:
                fields[op] = fields[op] | {'ultrafast'}
            if not isinstance(op,str) or op not in fields or set(args)!=fields[op]:
                raise AssistantError('management_operation','未知管理操作或参数')
            if op.startswith('config.') and (not isinstance(args['kind'],str) or
                    (op=='config.patch' and (not isinstance(args['changes'],dict) or not isinstance(args['expected'],str)))):
                raise AssistantError('management_request','配置请求格式无效')
            if op.startswith('records.') or op=='foundation.add':
                self._record_arguments(op,args)
            if op=='baseline.publish':
                expected=args['expected']
                if (not isinstance(expected,dict) or set(expected)!={'pins','foundation_count'}
                        or not isinstance(expected['pins'],list) or type(expected['foundation_count']) is not int
                        or any(not isinstance(pin,dict) or set(pin)!={'kind','id','version','policy_version'}
                            or pin['kind'] not in ('persona','memories')
                            or pin['id'] is not None and not isinstance(pin['id'],str)
                            or not isinstance(pin['version'],str) or not isinstance(pin['policy_version'],str)
                            for pin in expected['pins'])):
                    raise AssistantError('management_request','共用设定预览格式无效，请重新读取')
            if op=='backups.preview' and not isinstance(args['name'],str):
                raise AssistantError('management_request','备份名称格式无效')
            if op.startswith('runtime.') and op not in ('runtime.status','runtime.stop_all') and (not isinstance(args['platform'],str)
                    or op=='runtime.control' and not isinstance(args['action'],str)):
                raise AssistantError('management_request','平台请求格式无效')
            if op=='session.status':
                result = self.status()
            elif op=='session.unlock':
                result = self.unlock(args['password'])
            elif op=='session.lock':
                result = self.lock()
            elif op=='lock.enable':
                result = self.enable_lock(args['password'])
            elif op=='lock.disable':
                result = self.disable_lock(args['password'])
            else:
                request = self.request()
                if op in {'context.source_save','context.preset_save','context.preset_apply',
                          'records.restore_version','facts.restore_version','library.replace','library.restore',
                          'config.patch','records.correct','records.advanced_commit','records.delete','growth.rebuild','facts.retention_save','facts.confirm','facts.correct','foundation.add','baseline.publish','relationship.save','relationship.clear','relationship.link_confirm','relationship.link_revoke','audience.save','media.configure','media.preserve','media.budget.configure','media.capacity.configure','media.voice.configure','stickers.import_file','stickers.configure','files.import_file','files.configure','files.settings.configure','reply_quota.account_save','reply_quota.scope_save','reply_quota.scope_reset','reply_exclusion.exclude','reply_exclusion.restore','sharing.commit','relations.commit'}:
                    if self.store.state('desktop-last-restore',{}).get('status')=='assets_pending':
                        raise AssistantError('restore_pending','资料恢复尚未完成，请重新启动应用后台以继续补齐图片')
                    fingerprint = hashlib.sha256(json.dumps({'operation':op,'arguments':args},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                    key = 'desktop-request:'+message['request_id']
                    with self.store.lock, self.store.db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        self.authority.require(request,administrator=True)
                        prior = self.store.state(key,db=db)
                        if prior:
                            if prior['fingerprint'] != fingerprint:
                                raise AssistantError('request_id_conflict','请求标识已用于另一项修改')
                            result = prior['result']
                        else:
                            result = self._operation(request,op,args)
                            self.store.set_state(key,{'fingerprint':fingerprint,'result':result},db)
                    if op == 'audience.save' and not prior and result['scope']['platform']=='wechat' and self.runtime:
                        from .desktop_wechat import adapter
                        self.runtime.configure_platform("wechat",adapter(self.store,self.installation,self.wechat_resources))
                else:
                    result = self._operation(request,op,args)
            return {'ok':True,'result':result}
        except AssistantError as error:
            if error.code != 'database_error':
                self.repository.audit(self.actor.key,'desktop_request_denied',error.code)
            return {'ok':False,'error':error.code,'message':str(error)}

    def _record_arguments(self, op, args):
        """Validate transport types before entering the shared record operations."""
        for name in ('kind','after','text','expected_version','token','content'):
            if op == 'records.versions' and name == 'after' and args.get('after') is None:
                continue
            if name in args and not isinstance(args[name],str):
                raise AssistantError('management_request','资料请求格式无效')
        if 'identifier' in args and args['identifier'] is not None and not isinstance(args['identifier'],str):
            raise AssistantError('management_request','资料标识格式无效')
        if 'expected' in args and args['expected'] is not None and not isinstance(args['expected'],str):
            raise AssistantError('management_request','资料版本格式无效')
        if 'value' in args and not isinstance(args['value'],dict):
            raise AssistantError('management_request','资料内容格式无效')
        if 'limit' in args and (type(args['limit']) is not int or not 1<=args['limit']<=100):
            raise AssistantError('management_request','每次只能查询 1 至 100 条资料')
        if op=='foundation.add' and (not args['identifier'] or not args['expected']):
            raise AssistantError('management_request','请提供基础记忆标识和人物卡版本')

    def _operation(self, request, op, args):
        from . import desktop_library_versions, desktop_record_versions
        if op in desktop_record_versions.FIELDS:
            return desktop_record_versions.operate(self, request, op, args)
        if op in desktop_library_versions.FIELDS:
            return desktop_library_versions.operate(self, request, op, args)
        if op.startswith(("workspace.", "context.", "learning.")):
            from .desktop_workspace import operate
            return operate(self, request, op, args)
        if op.startswith("bili."):
            self.authority.require(request,administrator=True)
            if self.bili is None:
                raise AssistantError("bili_unavailable","B 站账号服务尚未就绪。")
            if op in ("bili.policy","bili.policy_save"):
                from .desktop_bili_policy import manage
                return manage(self,save=op=="bili.policy_save",**args)
            result = self.bili.accept_browser(**args) if op == "bili.browser_complete" else self.bili.operate(op.split(".")[1],**args)
            if self.runtime and result["current"] and "bilibili" not in self.runtime.reconfiguring:
                core = self.runtime.cores.get("bilibili")
                if not core or [a.account for a in core.platforms.all()] != [result["current"]["uid"]]:
                    from .desktop_bili_connector import adapter
                    self.runtime.configure_platform("bilibili",adapter(self.bili))
            return result
        if op in ('room.history','room.search','room.queue','room.cancel','room.post','room.post_files','room.preview'):
            token = args['session']
            with self.store.lock:
                self.room.sessions.request(token,'role')
                if op in ('room.history','room.search'):
                    result = self.room.page(token,before=args['before'],query=args.get('query',''),limit=12)
                    # Large Chinese/emoji pages must fit the native UTF-8/escaped JSON pipe.
                    # Drop only the oldest rows and keep their paging boundary reachable.
                    rows = result['messages']
                    while len(rows)>1 and len(json.dumps(result))>220000:
                        rows.pop(0)
                        result['next'] = rows[0]['cursor']
                elif op == 'room.queue':
                    result = {'messages':self.room.queue(token)}
                elif op == 'room.cancel':
                    result = self.room.cancel(token,args['id'])
                elif op == 'room.preview':
                    result = self.room.preview(token,args['turn'],args['image'])
                else:
                    if self.room.core is None or self.store.state('core-run:local',{}).get('status') != 'active':
                        raise AssistantError('room_paused','请先在平台运行中启动本机会客室')
                    if op == 'room.post_files':
                        result = self.room.post_files(token,args['text'],args['nonce'],args['files'])
                    else:
                        result = self.room.post(token,args['text'],args['nonce'])
                    if self.runtime and self.runtime.scheduler:
                        self.runtime.scheduler.wake('local')
                self.room.sessions.request(token,'role')
                return result
        if op in ('wechat.targets.saved','wechat.targets.save'):
            import asyncio

            from .desktop_wechat_targets import save, saved
            self.authority.require(request, administrator=True)
            if op=='wechat.targets.saved':
                return saved(self, **args)
            if self.store.state('desktop-last-restore',{}).get('status')=='assets_pending':
                raise AssistantError('restore_pending', '请等待资料恢复完成后保存名单')
            return asyncio.run(save(self, request, **args))
        if op in ('wechat.directory','wechat.directory_page','wechat.directory_sync','wechat.targets.verify'):
            import asyncio

            from .desktop_wechat import directory, directory_page, directory_sync, verify_targets
            handler = {'wechat.directory_sync': directory_sync, 'wechat.directory': directory, 'wechat.directory_page': directory_page,
                       'wechat.targets.verify': verify_targets}[op]
            return asyncio.run(handler(self, request, **args))
        if op in ('wechat.profiles','wechat.select'):
            from .desktop_wechat import select_account, selection, version_status
            self.authority.require(request, administrator=True)
            if op == 'wechat.profiles':
                return {**selection(self.store, self.installation), "client": version_status()}
            return select_account(self, request, **args)
        if op == 'wechat.accounts':
            import asyncio

            from .desktop_wechat import detect_accounts

            self.authority.require(request, administrator=True)
            result = asyncio.run(detect_accounts(self.wechat_resources))
            self.authority.require(request, administrator=True)
            self.wechat_detected = {request.session_id: tuple(result["accounts"])}
            from .desktop_wechat import selection
            return {**result, "profiles": selection(self.store, self.installation)}
        if op == 'wechat.account_details':
            import asyncio

            from .desktop_wechat import account_details

            return asyncio.run(account_details(self, request, **args))
        if op.startswith('audience.'):
            self.authority.require(request,administrator=True)
            if op=='audience.status':
                return self.audiences.status(args['platform'])
            if op=='audience.preview':
                return self.audiences.prepare(request,**args)
            return self.audiences.save(request,args['token'])
        if op.startswith('reply_quota.'):
            from .desktop_reply_quota import account_save, scope_reset, scope_save, scopes, summary

            # Saved registration is the only evidence; these operations never reach
            # a connector, never change authorizations and never start a platform.
            self.authority.require(request, administrator=True)
            if op=='reply_quota.summary':
                return summary(self,request,**args)
            if op=='reply_quota.scopes':
                return scopes(self,request,**args)
            if op=='reply_quota.account_save':
                return account_save(self,request,**args)
            if op=='reply_quota.scope_save':
                return scope_save(self,request,**args)
            return scope_reset(self,request,**args)
        if op.startswith('sharing.'):
            # Only the existing access rules are reused: the read decision stays
            # AccessService, a private-chat grant stays AdminService.authorize and a
            # classification change keeps every stored source, owner and expiry.
            self.authority.require(request, administrator=True)
            handlers = {'sharing.records': self.sharing.listing,
                        'sharing.record': self.sharing.detail,
                        'sharing.targets': self.sharing.targets,
                        'sharing.grants': self.sharing.grants,
                        'sharing.preview_grant': self.sharing.preview_grant,
                        'sharing.preview_classify': self.sharing.preview_classify,
                        'sharing.preview_revoke': self.sharing.preview_revoke,
                        'sharing.commit': self.sharing.commit,
                        'sharing.cancel': self.sharing.cancel}
            return handlers[op](request, **args)
        if op.startswith('relations.'):
            # Only existing claims are adjudicated: the core RelationService stays
            # the single writer, the statement/evidence/owners/pins/expiry are kept,
            # and no account link, grant or other record permission is touched.
            self.authority.require(request, administrator=True)
            handlers = {'relations.list': self.relations.listing,
                        'relations.detail': self.relations.detail,
                        'relations.history': self.relations.history,
                        'relations.preview': self.relations.preview,
                        'relations.commit': self.relations.commit,
                        'relations.cancel': self.relations.cancel}
            return handlers[op](request, **args)
        if op.startswith('reply_exclusion.'):
            from .desktop_reply_exclusion import exclude, restore

            # Offline single-conversation stop and lift: saved registration is the only
            # evidence, no connector is contacted, and nothing else is written.
            self.authority.require(request, administrator=True)
            handler = exclude if op == 'reply_exclusion.exclude' else restore
            return handler(self, request, **args)
        if op.startswith('reply_trace.'):
            from .desktop_reply_trace import detail, listing

            # Read-only trace: the core listing/detail are reused as they are. This
            # branch is deliberately outside the change set above, so no
            # desktop-request idempotency row, no lock and no audit write happen.
            self.authority.require(request, administrator=True)
            return (listing(self, request, args['limit']) if op == 'reply_trace.list'
                    else detail(self, request, args['identifier']))
        if op.startswith('growth.'):
            handlers = {'growth.preview': self.growth.preview, 'growth.rebuild': self.growth.rebuild,
                        'growth.archives': self.growth.archives, 'growth.archive': self.growth.archive}
            return handlers[op](request, **args)
        if op.startswith('facts.'):
            if op in ('facts.retention','facts.retention_save'):
                from companion_core.fact_retirement import configure, settings
                self.authority.require(request, administrator=True)
                if op == 'facts.retention':
                    return {**settings(self.store), 'last_cleanup': self.store.state('fact-cleanup-last'),
                            'maintenance_error': (self.runtime.scheduler.maintenance_error
                                if self.runtime is not None and self.runtime.scheduler is not None else None)}
                return configure(self.store, **args, actor=request.actor)
            from companion_core.fact_management import FactManagement
            if (not isinstance(args['identifier'],str) or not args['identifier'].startswith('fact-')
                    or op=='facts.history' and (not isinstance(args['after'],str)
                        or args['at'] is not None and not isinstance(args['at'],str))
                    or op in ('facts.confirm','facts.correct') and not isinstance(args['expected'],str)):
                raise AssistantError('management_request','个人事实请求格式无效')
            facts = FactManagement(self.admin)
            if op=='facts.history':
                return facts.history(request,args['identifier'],after=args['after'],limit=20,at=args['at'])
            if op=='facts.correct':
                return facts.correct(request,**args)
            return facts.confirm(request,args['identifier'],args['expected'],args['reason'],args['supersedes'])
        if op.startswith('relationship.'):
            from companion_core import manual_identity, person_baseline
            self.authority.require(request, administrator=True)
            if any(not isinstance(args[k], str) for k in ('after','anchor','expected','left','right','identifier','reason') if k in args):
                raise AssistantError('management_request','基础关系请求格式无效')
            if op in ('relationship.link_preview','relationship.link_confirm'):
                return manual_identity.manage(self.admin,request,**args)
            if op=='relationship.links':
                return manual_identity.listing(self.store,args['after'])
            if op=='relationship.link_detail':
                return manual_identity.detail(self.store,args['identifier'])
            if op=='relationship.link_revoke':
                return manual_identity.revoke(self.admin,request,**args)
            if op=='relationship.read':
                return person_baseline.read(self.store)
            if op=='relationship.accounts':
                return person_baseline.accounts(self.store,args['after'])
            if op=='relationship.person':
                return {'members':person_baseline.members(self.store,args['anchor'])}
            if op=='relationship.clear':
                return person_baseline.clear(self.admin,request,args['expected'])
            return person_baseline.save(self.admin,request,**args)
        if op.startswith('baseline.'):
            from companion_core import baseline

            self.authority.require(request,administrator=True)
            if op=='baseline.preview':
                return {**baseline.preview(self.store),'shared':baseline.enabled(self.store)}
            return baseline.publish(self.store,args['expected'],request.actor)
        if op.startswith('files.'):
            from companion_core.file_library import FileLibrary
            self.authority.require(request, administrator=True)
            library = FileLibrary(self.store)
            from companion_qq.file_replies import FileReplySettings
            if op == 'files.settings':
                return FileReplySettings(self.store).read()
            if op == 'files.settings.configure':
                return FileReplySettings(self.store).configure(**args, actor=request.actor)
            if op == 'files.list':
                return {**library.listing(**args), 'settings': FileReplySettings(self.store).read()}
            if op == 'files.targets':
                return library.targets()
            if op == 'files.import_file':
                return library.import_file(**args, actor=request.actor)
            if op == 'files.configure':
                return library.configure(**args, actor=request.actor)
        if op.startswith('stickers.'):
            from companion_core.sticker_library import StickerLibrary

            self.authority.require(request, administrator=True)
            library = StickerLibrary(self.store)
            if op == 'stickers.list':
                return library.listing(**args)
            if op == 'stickers.import_file':
                return library.import_file(**args, actor=request.actor)
            if op == 'stickers.configure':
                return library.configure(**args, actor=request.actor)
        if op.startswith('media.'):
            from companion_core.media_budget import MediaBudgetSettings
            from companion_core.media_originals import MediaOriginals

            self.authority.require(request, administrator=True)
            media = MediaOriginals(self.store)
            if op == 'media.capacity':
                return media.capacity()
            if op == 'media.capacity.configure':
                return media.configure_capacity(**args, actor=request.actor)
            from companion_qq.voice_settings import VoiceSettings

            if op == 'media.voice':
                return VoiceSettings(self.store).read()
            if op == 'media.voice.configure':
                return VoiceSettings(self.store).configure(**args, actor=request.actor)
            if op == 'media.budget':
                return MediaBudgetSettings(self.store).read()
            if op == 'media.budget.configure':
                return MediaBudgetSettings(self.store).configure(**args, actor=request.actor)
            if op == 'media.settings':
                return media.settings()
            if op == 'media.configure':
                return media.configure(**args, actor=request.actor)
            if op == 'media.list':
                result = media.listing(**args)
                result['voice'] = VoiceSettings(self.store).read()
                result['budget'] = MediaBudgetSettings(self.store).read()
                result['capacity'] = media.capacity()
                result['maintenance_error'] = (self.runtime.scheduler.maintenance_error
                    if self.runtime is not None and self.runtime.scheduler is not None else None)
                return result
            if op == 'media.preserve':
                return media.preserve(**args, actor=request.actor)
        if op.startswith('qq.'):
            self.authority.require(request,administrator=True)
            if self.qq is None:
                raise AssistantError('qq_management_unavailable','QQ 登录管理尚未就绪。')
            operation=op.removeprefix('qq.')
            if operation=='status':
                return self.qq.status()
            if operation in ('login','refresh','password'):
                result=self.qq.begin(operation,**args)
            elif operation=='cancel':
                result=self.qq.cancel()
            elif operation=='targets.verify':
                result=self.qq.verify_targets(**args)
            elif operation=='directory':
                result=self.qq.load_directory(**args)
            elif operation=='targets.save':
                result=self.qq.save_targets(**args,actor=request.actor)
            else:
                result=self.qq.select(**args)
            if operation in ('select','targets.save') and self.runtime is not None:
                self.runtime.configure_qq()
            self.repository.audit(request.actor,'desktop_qq_'+operation,self.installation)
            return result
        if op.startswith('model.'):
            self.authority.require(request,administrator=True)
            if self.codex is None:
                raise AssistantError('model_unavailable','模型登录服务尚未就绪')
            operation=op.removeprefix('model.')
            if operation in ('catalog','configure'):
                model = self.runtime.model if self.runtime else None
                if model is None or not hasattr(model, 'catalog'):
                    raise AssistantError('model_unavailable','模型服务尚未就绪')
                if operation == 'catalog':
                    if type(args['refresh']) is not bool:
                        raise AssistantError('management_request','刷新参数无效')
                    return model.catalog.status(self.store, refresh=args['refresh'])
                return model.catalog.configure(self.store, **args)
            if operation=='status':
                return {**self.codex.status(), 'handoff_pending':bool(
                    self.runtime and self.runtime.handoff_pending())}
            if operation in ('add','select'):
                if not self.codex.status().get('multi_account'):
                    raise AssistantError('model_operation','当前登录方式不支持保存多个账号')
                result=self.codex.begin(operation,**args)
                self.repository.audit(request.actor,'desktop_model_'+operation,self.installation)
                return result
            if operation in ('refresh','login','logout'):
                result=self.codex.begin(operation)
                self.repository.audit(request.actor,'desktop_model_'+operation,self.installation)
                return result
            if operation=='cancel':
                result=self.codex.cancel()
                self.repository.audit(request.actor,'desktop_model_cancel_requested' if result['cancel_requested']
                                      else 'desktop_model_cancel_idle',self.installation)
                return result
            return {'status':self.codex.status,'cancel':self.codex.cancel,'open':self.codex.open_login}[operation]()
        if op=='notifications.page':
            return self.notifications.page(request, **args)
        if op=='notifications.update':
            return self.notifications.update(request, **args)
        if op=='notifications.list':
            return self.notifications.listing(request)
        if op=='notifications.read':
            return self.notifications.mark_read(request,args['identifier'])
        if op.startswith('tasks.'):
            if self.runtime is None:
                raise AssistantError('runtime_unavailable','应用运行层尚未就绪')
            return {'tasks.list': self.runtime.manual.listing, 'tasks.submit': self.runtime.manual.submit,
                    'tasks.detail': self.runtime.manual.detail,
                    'tasks.control': self.runtime.manual.control}[op](**args)
        if op.startswith('runtime.'):
            if self.runtime is None:
                raise AssistantError('runtime_unavailable','应用运行层尚未就绪')
            if op=='runtime.status':
                return self.runtime.status()
            if op=='runtime.stop_all':
                return self.runtime.stop_all(request)
            return {'runtime.preference':self.runtime.preference,'runtime.control':self.runtime.control}[op](request,**args)
        if op.startswith('backups.'):
            return {'backups.list':self.backups.listing,'backups.create':self.backups.create,
                    'backups.preview':self.backups.prepare}[op](**args)
        operations = {'config.read':self.admin.read_config,'config.patch':self.admin.patch_config,
                      'records.list':self.records.listing,'records.read':self.records.detail,
                      'records.explain':self.records.explain,
                      'records.correct':self.records.correct,'records.export':self.records.export,
                      'records.import_preview':self.records.import_preview,
                      'records.advanced_export':self.records.advanced.export,
                      'records.advanced_preview':self.records.advanced.preview,
                      'records.advanced_commit':self.records.advanced.commit,
                      'records.advanced_cancel':self.records.advanced.cancel,
                      'records.advanced_changes':self.records.advanced.changes,
                      'records.delete_preview':self.records.delete_preview,'records.delete':self.records.delete,
                      'foundation.add':self.records.add_foundation,'actors.list':self.records.actors}
        return operations[op](request,**args)
