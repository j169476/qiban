"""Real PostgreSQL authorization tests using a disposable desktop test installation."""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from keyring.backends.Windows import WinVaultKeyring

from companion_core.access import RequestContext
from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore
from qiban.desktop_management import LOCK, OWNER, DesktopManagement, provision_owner


@pytest.fixture
def desktop_store(tmp_path, pg_profile):
    if not os.environ.get('QIBAN_DESKTOP_TEST_ROOT'):
        pytest.skip('Requires a disposable running desktop test installation')
    root=Path(os.environ['QIBAN_DESKTOP_TEST_ROOT'])
    state=json.loads((root/'installation.json').read_text(encoding='utf8'))
    credential=json.loads(WinVaultKeyring().get_password('qiban-desktop-probe-storage',state['installation']))
    options={'host':'127.0.0.1','port':state['port'],'user':'qiban_setup','password':credential['bootstrap_password'],'dbname':'qiban'}
    schema='desktop_test_'+uuid.uuid4().hex
    with psycopg.connect(**options,autocommit=True) as db:
        db.execute('CREATE SCHEMA '+schema)
    key=secrets.token_bytes(32)
    store=PostgresStore(tmp_path,{**options,'options':'-c search_path='+schema},key,profile=pg_profile)
    store._test_key=key
    try:
        store.migrate()
        assert store.initialize()['ok']
        yield store
    finally:
        store.database.close()
        with psycopg.connect(**options,autocommit=True) as db:
            db.execute('DROP SCHEMA '+schema+' CASCADE')


@pytest.fixture
def service(desktop_store):
    provision_owner(desktop_store,'installation-test','S-1-5-21-test-owner')
    return DesktopManagement(desktop_store,'installation-test','S-1-5-21-test-owner')


def call(service,op,args=None,request_id=None,**extra):
    return service.dispatch({'action':'management','operation':op,'arguments':args or {},
                             'request_id':request_id or uuid.uuid4().hex,**extra})


def memory_args(identifier='desktop-memory',content='合成的桌面资料'):
    return {'kind':'memories','identifier':identifier,'expected':None,
            'value':{'schema_version':1,'id':identifier,'kind':'user_preference','content':content,
                     'confidence':'observed','tags':[],'locked':False,
                     'source':{'type':'user_instruction','reference':'desktop-test','recorded_at':'2026-10-30T00:00:00Z'}}}


def test_siwc_credentials_encrypted_excluded_from_backup_and_preserved_on_restore(desktop_store, monkeypatch):
    from companion_core import postgres_transfer
    from qiban.desktop_siwc import KEY, DesktopSiwc

    broker = DesktopSiwc(desktop_store.root, desktop_store)
    broker._save({'host':'test-host', 'session':{'refresh_token':'SYNTHETIC-RENEWABLE-SECRET'}})
    with desktop_store.db() as db:
        raw = db.execute('SELECT value FROM state WHERE key=?', (KEY,)).fetchone()[0]
    assert 'SYNTHETIC-RENEWABLE-SECRET' not in str(raw)
    key = secrets.token_bytes(32)
    blob = postgres_transfer.create(desktop_store, key)
    snapshot = postgres_transfer.unpack(blob, key)
    assert KEY not in str(snapshot['tables']['state'])
    newer = {'host':'test-host','session':{'refresh_token':'NEW-ROTATED-SECRET'}}
    broker._save(newer)
    result = postgres_transfer.restore(desktop_store, blob, key, expected=postgres_transfer.fingerprint(desktop_store))
    assert result['restored'] and broker._load() == newer
    # A legacy/imported snapshot containing a renewable session cannot resurrect a signed-out account.
    with monkeypatch.context() as patch:
        patch.setattr(postgres_transfer, 'LOCAL_STATE_KEYS', frozenset())
        legacy = postgres_transfer.create(desktop_store, key)
    signed_out = {'host':'test-host'}
    broker._save(signed_out)
    postgres_transfer.restore(desktop_store, legacy, key, expected=postgres_transfer.fingerprint(desktop_store))
    assert broker._load() == signed_out


def restore_call(service,op,args=None):
    return service.backups.dispatch({'action':'desktop-restore','operation':op,'arguments':args or {}})


def seed_accounts(store):
    from qiban.desktop_siwc import KEY
    from qiban.desktop_siwc_accounts import registration_id

    accounts={}
    for number in (1,2):
        account={'client_id':f'client-{number}','subject':f'subject-{number}','number':number,
                 'email':'synthetic@example.invalid','session':{'access_token':f'access-{number}',
                 'refresh_token':f'PRIVATE-REFRESH-{number}','expires_at':1001,
                 'scopes':['chatgpt.tokens.use.direct']}}
        accounts[registration_id(account)]=account
    book={'format':2,'revision':1,'host':'host','active':next(iter(accounts)),'accounts':accounts}
    store.set_state(KEY,book)
    return book


def test_saved_account_selection_requires_admin_and_current_revision(service):
    from qiban.desktop_siwc import KEY
    from qiban.desktop_siwc_accounts import DesktopSiwcAccounts

    book=seed_accounts(service.store)
    def no_network(*args):
        raise AssertionError('Selection must not access network')
    broker=DesktopSiwcAccounts(service.store.root,service.store,transport=no_network,clock=lambda:1000)
    service.codex=broker
    target=list(book['accounts'])[1]
    args={'account_id':target,'expected':1}
    assert not call(service,'model.select',{**args,'automatic':True})['ok']
    service.enable_lock('Synthetic-Account-Lock-Password')
    service.lock()
    assert call(service,'model.select',args)['error']=='desktop_locked'
    assert service.store.state(KEY)==book
    service.unlock('Synthetic-Account-Lock-Password')
    assert call(service,'model.select',args)['ok']
    broker.thread.join(5)
    assert not broker.thread.is_alive()
    changed=service.store.state(KEY)
    assert changed['active']==target and changed['accounts']==book['accounts']
    assert call(service,'model.select',{'account_id':book['active'],'expected':1})['ok']
    broker.thread.join(5)
    assert broker.status()['code']=='model_account_changed'
    assert service.store.state(KEY)==changed


def test_multi_account_restore_preserves_selection_and_rotated_credentials(desktop_store):
    from companion_core import postgres_transfer
    from qiban.desktop_siwc import KEY
    from qiban.desktop_siwc_accounts import DesktopSiwcAccounts

    book=seed_accounts(desktop_store)
    key=secrets.token_bytes(32)
    blob=postgres_transfer.create(desktop_store,key)
    assert KEY not in str(postgres_transfer.unpack(blob,key)['tables']['state'])
    other=list(book['accounts'])[1]
    untouched=book['accounts'][other]
    calls=[]
    def exchange(url,form=None):
        calls.append(form['refresh_token'])
        return {'access_token':'rotated-access','refresh_token':'ROTATED-PRIVATE-REFRESH',
                'expires_in':3600,'token_type':'Bearer'}
    broker=DesktopSiwcAccounts(desktop_store.root,desktop_store,transport=exchange,clock=lambda:1000)
    write=broker._retain_rotation
    def interrupt(value):
        if value['renewal']['status']=='complete':
            raise OSError('Synthetic interruption')
        write(value)
    broker._retain_rotation=interrupt
    broker.begin('refresh')
    broker.thread.join(5)
    assert desktop_store.state(KEY)['accounts'][book['active']]['renewal']['status']=='received'
    reopened=DesktopSiwcAccounts(desktop_store.root,desktop_store,transport=exchange,clock=lambda:1000)
    reopened.begin('refresh')
    reopened.thread.join(5)
    assert calls==['PRIVATE-REFRESH-1']
    current=desktop_store.state(KEY)
    assert current['accounts'][other]==untouched
    assert current['accounts'][book['active']]['session']['refresh_token']=='ROTATED-PRIVATE-REFRESH'
    reopened.begin('select',account_id=other,expected=current['revision'])
    reopened.thread.join(5)
    current=desktop_store.state(KEY)
    assert current['active']==other
    postgres_transfer.restore(desktop_store,blob,key,expected=postgres_transfer.fingerprint(desktop_store))
    assert desktop_store.state(KEY)==current
    with desktop_store.db() as db:
        raw=db.execute('SELECT value FROM state WHERE key=?',(KEY,)).fetchone()[0]
    assert 'PRIVATE-REFRESH' not in str(raw)


def test_siwc_rotating_receipt_survives_failed_commit_and_restart(desktop_store, tmp_path):
    from qiban.desktop_siwc import KEY, DesktopSiwc

    now=1000
    calls=[]
    def exchange(url,form=None):
        calls.append(form['refresh_token'])
        return {'access_token':'SYNTHETIC-NEW-ACCESS','refresh_token':'SYNTHETIC-NEW-REFRESH',
                'expires_in':3600,'token_type':'Bearer'}
    broker=DesktopSiwc(tmp_path/'auth',desktop_store,transport=exchange,clock=lambda:now)
    broker._save({'host':'host','client_id':'client','subject':'subject','session':{
        'access_token':'old','refresh_token':'old-refresh','expires_at':1001,
        'id_token':'old-verified-id','scopes':['chatgpt.tokens.use.direct']}})
    write=broker._retain_rotation
    def fail_commit(value):
        if value['renewal']['status']=='complete':
            raise OSError('Injected commit interruption')
        write(value)
    broker._retain_rotation=fail_commit
    broker.begin('refresh')
    broker.thread.join(5)
    assert not broker.thread.is_alive() and broker._load()['renewal']['status']=='received'
    recovered=DesktopSiwc(tmp_path/'auth',desktop_store,transport=exchange,clock=lambda:now)
    recovered.begin('refresh')
    recovered.thread.join(5)
    assert not recovered.thread.is_alive() and recovered.status()['renewal_status']=='complete'
    assert recovered._load()['session']['refresh_token']=='SYNTHETIC-NEW-REFRESH' and calls==['old-refresh']
    with desktop_store.db() as db:
        raw=db.execute('SELECT value FROM state WHERE key=?',(KEY,)).fetchone()[0]
    assert 'SYNTHETIC-NEW' not in str(raw)


@pytest.mark.parametrize('crash_stage',['pending','received'])
def test_siwc_real_process_exit_does_not_replay_refresh(desktop_store,tmp_path,crash_stage):
    from qiban.desktop_siwc import DesktopSiwc

    auth_root=tmp_path/'process-auth'
    broker=DesktopSiwc(auth_root,desktop_store,clock=lambda:1000)
    broker._save({'host':'host','client_id':'client','subject':'subject','session':{
        'access_token':'old','refresh_token':'old-refresh','expires_at':1001,
        'id_token':'old-id','scopes':['chatgpt.tokens.use.direct']}})
    # Only this fixture's database credentials travel over stdin; never arguments or a file.
    script='''import base64,json,os,sys
from companion_core.postgres import PostgresStore
from qiban.desktop_siwc import DesktopSiwc
v=json.load(sys.stdin)
store=PostgresStore(v['root'],v['options'],base64.b64decode(v['key']))
def exchange(*args):
    return {'access_token':'rotated-access','refresh_token':'rotated-refresh','expires_in':3600,'token_type':'Bearer'}
broker=DesktopSiwc(v['auth'],store,transport=exchange,clock=lambda:1000)
write=broker._retain_rotation
def interrupted(value):
    write(value)
    if value['renewal']['status']==v['stage']: os._exit(93)
broker._retain_rotation=interrupted
broker._operate('refresh')
'''
    payload={'root':str(desktop_store.root),'options':desktop_store.database._options,
        'key':base64.b64encode(desktop_store._test_key).decode(),'auth':str(auth_root),'stage':crash_stage}
    result=subprocess.run([sys.executable,'-c',script],input=json.dumps(payload),text=True,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==93
    requests=[]
    def unexpected(*args):
        requests.append(True)
        raise AssertionError('Refresh must not replay')
    recovered=DesktopSiwc(auth_root,desktop_store,transport=unexpected,clock=lambda:1000)
    recovered.begin('refresh')
    recovered.thread.join(5)
    assert not recovered.thread.is_alive() and not requests
    assert recovered.status()['renewal_status']==('pending' if crash_stage=='pending' else 'complete')


def backup_then_change(service):
    original=service.store.read('preferences')
    backup=call(service,'backups.create')['result']
    changed=call(service,'config.patch',{'kind':'preferences','changes':{'topics':['备份后的修改']},'expected':original['version']})
    assert changed['ok']
    return original,backup,changed['result']


def prepare_restore(service,backup):
    preview=call(service,'backups.preview',{'name':backup['name']})
    assert preview['ok'],preview
    start=restore_call(service,'begin',{'token':preview['result']['token']})
    assert start['ok'],start
    return start['result']['challenge']


@pytest.mark.parametrize('locked',[False,True])
def test_restore_requires_private_challenge_and_preserves_current_owner_lock(service,locked):
    original,backup,_=backup_then_change(service)
    if locked:
        service.enable_lock('Current-Password-After-Backup!')
    owner,lock=service.store.state(OWNER),service.store.state(LOCK)
    issued=service.request()
    challenge=prepare_restore(service,backup)
    assert call(service,'backups.complete',{'challenge':challenge})['error']=='management_operation'
    # The rejected page request changes audit history, so its old preview must also be rejected.
    assert restore_call(service,'complete',{'challenge':challenge})['error']=='edit_conflict'
    challenge=prepare_restore(service,backup)
    result=restore_call(service,'complete',{'challenge':challenge})
    assert result['ok'],result
    assert result['result']['last_restore']['status']=='complete'
    assert service.store.read('preferences')==original
    assert service.store.state(OWNER)==owner
    assert service.store.state(LOCK)=={**lock,'revision':lock['revision']+1}
    with pytest.raises(AssistantError):
        service.authority.require(issued,administrator=True)
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid)
    assert reopened.status()['locked']==locked
    if locked:
        reopened.unlock('Current-Password-After-Backup!')
    assert restore_call(service,'complete',{'challenge':challenge})['error']=='restore_verification_expired'
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM state WHERE key LIKE 'desktop-request:%'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='desktop_restore_verified_windows'").fetchone()[0]==1


def test_restore_cancel_does_not_change_any_database_state(service):
    from companion_core.postgres_transfer import fingerprint

    _,backup,changed=backup_then_change(service)
    before=fingerprint(service.store)
    challenge=prepare_restore(service,backup)
    assert restore_call(service,'cancel')['ok']
    assert restore_call(service,'complete',{'challenge':challenge})['error']=='restore_verification_expired'
    assert fingerprint(service.store)==before
    assert service.store.read('preferences')==changed


@pytest.mark.parametrize('change',['data','file','lock','restart','expiry','wrong_challenge'])
def test_restore_changed_or_stale_authorization_never_overwrites(service,change):
    _,backup,changed=backup_then_change(service)
    challenge=prepare_restore(service,backup)
    if change=='data':
        service.store.set_state('new-durable-state',{'updated':True})
    elif change=='file':
        path=service.backups.path(backup['name'])
        path.write_bytes(path.read_bytes()+b'changed')
    elif change=='lock':
        service.enable_lock('Current-Password-After-Backup!')
    elif change=='restart':
        service=DesktopManagement(service.store,service.installation,service.windows_sid)
    elif change=='expiry':
        service.backups.pending['expires']=0
    else:
        challenge='wrong-challenge'
    assert not restore_call(service,'complete',{'challenge':challenge})['ok']
    assert service.store.read('preferences')==changed


def test_restore_transaction_failure_leaves_current_data_and_lock(service,monkeypatch):
    from companion_core import asset_transfer

    _,backup,changed=backup_then_change(service)
    challenge=prepare_restore(service,backup)
    lock=service.store.state(LOCK)
    def fail(*args):
        raise AssistantError('injected','synthetic enqueue failure')
    monkeypatch.setattr(asset_transfer,'enqueue',fail)
    assert restore_call(service,'complete',{'challenge':challenge})['error']=='injected'
    assert service.store.read('preferences')==changed and service.store.state(LOCK)==lock
    assert service.store.state('desktop-last-restore') is None


def test_committed_restore_finishes_after_reopen_without_reauth_or_reapply(service,monkeypatch):
    from companion_core import asset_transfer
    from qiban.desktop_backups import finish_pending

    original,backup,_=backup_then_change(service)
    challenge=prepare_restore(service,backup)
    finish=asset_transfer.finish
    def fail(*args):
        raise AssistantError('injected','synthetic interruption after database commit')
    monkeypatch.setattr(asset_transfer,'finish',fail)
    assert restore_call(service,'complete',{'challenge':challenge})['error']=='injected'
    assert service.store.read('preferences')==original
    receipt=service.store.state('desktop-last-restore')
    assert receipt['status']=='assets_pending'
    assert call(service,'config.patch',{'kind':'preferences','changes':{'topics':['too early']},'expected':original['version']})['error']=='restore_pending'
    monkeypatch.setattr(asset_transfer,'finish',finish)
    assert finish_pending(service.store)=={**receipt,'status':'complete'}
    assert finish_pending(service.store)['id']==receipt['id']


def test_restore_preview_rejects_active_platform_and_path_escape(service):
    _,backup,_=backup_then_change(service)
    service.store.set_state('core-run:qq',{'status':'active'})
    assert call(service,'backups.preview',{'name':backup['name']})['error']=='restore_active'
    service.store.set_state('core-run:qq',{'status':'paused'})
    assert call(service,'backups.preview',{'name':'../foreign.ccbackup'})['error']=='backup_path'


def test_restored_active_platform_remains_paused(service):
    service.store.set_state('core-run:qq',{'status':'active','generation':'old-run','owner':'old-owner'})
    _,backup,_=backup_then_change(service)
    service.store.set_state('core-run:qq',{'status':'paused','generation':'old-run','owner':'old-owner'})
    challenge=prepare_restore(service,backup)
    result=restore_call(service,'complete',{'challenge':challenge})
    assert result['ok'],result
    assert service.store.state('core-run:qq')['status']=='paused'
    assert not result['result']['role_started']


@pytest.mark.parametrize('boundary',['before_commit','after_commit'])
def test_restore_process_exit_at_transaction_boundaries(service,boundary):
    from PIL import Image

    from qiban.desktop_backups import finish_pending

    asset=service.store.root/'assets/persona/synthetic.png'
    asset.parent.mkdir(parents=True)
    Image.new('RGB',(2,2),'purple').save(asset)
    original_image=asset.read_bytes()
    card=service.store.read('persona')
    service.admin.patch_config(service.request(),'persona',{'visual_identity':{'description':'合成测试图片',
        'references':[{'filename':asset.name,'sha256':hashlib.sha256(original_image).hexdigest(),'label':'test'}]}},card['version'])
    original,backup,changed=backup_then_change(service)
    Image.new('RGB',(2,2),'green').save(asset)
    changed_image=asset.read_bytes()
    script='''
import base64,json,os,sys
from companion_core import asset_transfer,postgres_transfer
from companion_core.postgres import PostgresStore
from qiban.desktop_management import DesktopManagement
args=json.loads(sys.stdin.read())
store=PostgresStore(args['root'],args['options'],base64.b64decode(args['key']))
management=DesktopManagement(store,'installation-test','S-1-5-21-test-owner')
preview=management.backups.prepare(args['name'])
challenge=management.backups.begin(preview['token'])['challenge']
if args['boundary']=='before_commit':
    restore=postgres_transfer.restore
    def interrupted(*a,finalize,**kw):
        def before_commit(db):
            finalize(db)
            os._exit(71)
        return restore(*a,finalize=before_commit,**kw)
    postgres_transfer.restore=interrupted
else:
    asset_transfer.finish=lambda store:os._exit(72)
management.backups.complete(challenge)
os._exit(99)
'''
    payload={'root':str(service.store.root),'options':service.store.database._options,
             'key':base64.b64encode(service.store._test_key).decode(),'name':backup['name'],'boundary':boundary}
    result=subprocess.run([sys.executable,'-c',script],input=json.dumps(payload),text=True,capture_output=True,
                          timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==(71 if boundary=='before_commit' else 72),result.stderr
    if boundary=='before_commit':
        assert service.store.read('preferences')==changed
        assert service.store.state('desktop-last-restore') is None
        assert asset.read_bytes()==changed_image
    else:
        assert service.store.read('preferences')==original
        assert service.store.state('desktop-last-restore')['status']=='assets_pending'
        assert asset.read_bytes()==changed_image
        with service.store.db() as db:
            assert db.execute('SELECT count(*) FROM asset_restore_jobs').fetchone()[0]==1
        assert finish_pending(service.store)['status']=='complete'
        assert service.store.read('preferences')==original
        assert asset.read_bytes()==original_image


def test_shared_records_legacy_and_desktop_versions_export_and_preview(service):
    from types import SimpleNamespace

    from companion_service.admin import Management

    legacy=Management(SimpleNamespace(store=service.store,admin=service.admin))
    args=memory_args()
    created=call(service,'records.correct',args)
    assert created['ok'],created
    target={'kind':args['kind'],'identifier':args['identifier']}
    assert call(service,'records.read',target)['result']==legacy.detail(service.request(),**target)
    detail=call(service,'records.read',target)
    assert json.loads(json.dumps(detail))==detail
    assert call(service,'records.list',{'kind':'memories','after':'','limit':1})['result']['records'][0]['id']==args['identifier']
    exported=call(service,'records.export',target)['result']
    version=created['result']['version']
    preview=call(service,'records.import_preview',{**target,'text':exported,'expected_version':version})
    assert preview['result']['data']==args['value'] and not preview['result']['written']
    changed=legacy.correct(service.request(),**{**args,'expected':version,'value':{**args['value'],'content':'另一入口修改'}})
    assert call(service,'records.correct',{**args,'expected':version})['error']=='edit_conflict'
    assert call(service,'records.import_preview',{**target,'text':exported,'expected_version':version})['error']=='edit_conflict'
    assert service.store.read('memories',args['identifier'])['version']==changed['version']


def test_records_write_receipt_survives_restart_and_lock(service):
    args,key=memory_args(),uuid.uuid4().hex
    first=call(service,'records.correct',args,key)
    assert first['ok'],first
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid)
    assert call(reopened,'records.correct',args,key)==first
    assert call(reopened,'records.correct',memory_args(content='changed'),key)['error']=='request_id_conflict'
    service.enable_lock('Synthetic-Record-Lock-2026!')
    service.lock()
    assert call(service,'records.correct',args,key)['error']=='desktop_locked'
    assert call(service,'records.read',{'kind':'memories','identifier':args['identifier']})['error']=='desktop_locked'
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='correct'").fetchone()[0]==1


def test_records_write_and_receipt_rollback_together(service,monkeypatch):
    original=service.store.set_state
    def fail(key,value,db=None):
        if key.startswith('desktop-request:'):
            raise AssistantError('injected','synthetic interruption')
        return original(key,value,db)
    monkeypatch.setattr(service.store,'set_state',fail)
    assert call(service,'records.correct',memory_args())['error']=='injected'
    assert not service.store.exists('memories','desktop-memory')
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='correct'").fetchone()[0]==0


def test_delete_preview_is_invalid_after_edit_and_is_not_reused(service):
    args=memory_args()
    first=call(service,'records.correct',args)['result']
    target={'kind':'memories','identifier':args['identifier']}
    preview=call(service,'records.delete_preview',target)['result']
    assert call(service,'records.correct',{**args,'expected':first['version'],'value':{**args['value'],'content':'changed'}})['ok']
    assert call(service,'records.delete',{'token':preview['token']})['error']=='edit_conflict'
    assert call(service,'records.delete',{'token':preview['token']})['error']=='delete_denied'
    assert service.store.exists('memories',args['identifier'])


@pytest.mark.parametrize('invalidation',['restart','lock','expiry'])
def test_delete_preview_bound_to_session_revision_and_expiry(service,invalidation,monkeypatch):
    args=memory_args()
    assert call(service,'records.correct',args)['ok']
    preview=call(service,'records.delete_preview',{'kind':'memories','identifier':args['identifier']})['result']
    if invalidation=='restart':
        service=DesktopManagement(service.store,service.installation,service.windows_sid)
    elif invalidation=='lock':
        service.enable_lock('Synthetic-Record-Lock-2026!')
        service.lock()
        service.unlock('Synthetic-Record-Lock-2026!')
    else:
        monkeypatch.setattr('companion_core.record_management.time.time',lambda:1e12)
    assert call(service,'records.delete',{'token':preview['token']})['error']=='delete_denied'
    assert service.store.exists('memories',args['identifier'])


def test_delete_commits_receipt_history_and_audit_once(service):
    args=memory_args()
    assert call(service,'records.correct',args)['ok']
    preview=call(service,'records.delete_preview',{'kind':'memories','identifier':args['identifier']})['result']
    key=uuid.uuid4().hex
    result=call(service,'records.delete',{'token':preview['token']},key)
    assert result['ok'] and result['result']['history_retained']
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid)
    assert call(reopened,'records.delete',{'token':preview['token']},key)==result
    assert call(reopened,'records.delete',{'token':preview['token']})['error']=='delete_denied'
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='delete'").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM content_history WHERE kind='memories' AND id=?",(args['identifier'],)).fetchone()[0]==1
    assert not service.store.exists('memories',args['identifier'])


def test_delete_receipt_failure_restores_record_and_requires_new_preview(service,monkeypatch):
    args=memory_args()
    assert call(service,'records.correct',args)['ok']
    target={'kind':'memories','identifier':args['identifier']}
    preview=call(service,'records.delete_preview',target)['result']
    original=service.store.set_state
    def fail(key,value,db=None):
        if key.startswith('desktop-request:'):
            raise AssistantError('injected','synthetic interruption')
        return original(key,value,db)
    monkeypatch.setattr(service.store,'set_state',fail)
    assert call(service,'records.delete',{'token':preview['token']})['error']=='injected'
    assert service.store.exists('memories',args['identifier'])
    assert call(service,'records.delete',{'token':preview['token']})['error']=='delete_denied'
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='delete'").fetchone()[0]==0
    monkeypatch.setattr(service.store,'set_state',original)
    refreshed=call(service,'records.delete_preview',target)['result']
    assert call(service,'records.delete',{'token':refreshed['token']})['ok']


def test_foundation_requires_publication_and_protects_shared_memory(service):
    from companion_core import baseline

    card=service.store.read('persona')
    args={'identifier':'foundation-test','content':'合成共用设定','expected':card['version']}
    assert call(service,'foundation.add',args)['error']=='baseline_unavailable'
    baseline.publish(service.store,baseline.preview(service.store),service.actor.key)
    assert call(service,'foundation.add',args)['ok']
    target={'kind':'memories','identifier':args['identifier']}
    assert call(service,'records.delete_preview',target)['error']=='baseline_locked'
    value=service.store.read('memories',args['identifier'])
    assert value['data']['locked']
    assert call(service,'records.correct',{**target,'expected':value['version'],
                'value':{**value['data'],'content':'明确修改的设定'}})['ok']
    assert service.store.read('persona')['data']['foundation_memories']==[args['identifier']]


@pytest.mark.parametrize('args',[
    {'kind':'memories','after':'','limit':True},
    {'kind':'memories','after':'','limit':101},
    {'kind':{},'after':'','limit':1},
    {'kind':'memories','after':[],'limit':1},
    {'kind':'memories','after':'','limit':1,'administrator':True},
])
def test_record_transport_rejects_invalid_arguments(service,args):
    assert not call(service,'records.list',args)['ok']


def test_records_require_sealed_admin_and_do_not_delete_config(service):
    role=service.authority.issue(actor=service.actor,conversation='role',audience='private',recipients=(service.actor,),
                                purpose='reply',session_id=service.session_id)
    with pytest.raises(AssistantError) as error:
        service.records.listing(role,'memories')
    assert error.value.code=='access_denied'
    assert call(service,'records.delete_preview',{'kind':'persona','identifier':None})['error']=='invalid_delete'
    assert call(service,'actors.list')['result']['limit']==200


def test_no_implicit_runtime_owner_and_no_other_windows_user(desktop_store):
    with pytest.raises(AssistantError,match='管理员'):
        DesktopManagement(desktop_store,'installation-test','S-1-5-21-test-owner')
    owner=provision_owner(desktop_store,'installation-test','S-1-5-21-test-owner')
    assert provision_owner(desktop_store,'installation-test','S-1-5-21-test-owner')==owner
    for install,sid in [('wrong','S-1-5-21-test-owner'),('installation-test','wrong')]:
        with pytest.raises(AssistantError) as error:
            DesktopManagement(desktop_store,install,sid)
        assert error.value.code=='desktop_owner_mismatch'
    assert desktop_store.state(OWNER)==owner


def test_only_sealed_administrator_can_use_shared_admin(service):
    request=service.request()
    service.admin.read_config(request,'persona')
    role=service.authority.issue(actor=service.actor,conversation='role',audience='private',recipients=(service.actor,),
                                 purpose='reply',session_id=service.session_id)
    candidates=[role,replace(role,administrator=True),replace(request,actor='model'),
                RequestContext(None,'fake','private',frozenset(),'management',1e12,'',0,True)]
    for candidate in candidates:
        with pytest.raises(AssistantError) as error:
            service.admin.read_config(candidate,'persona')
        assert error.value.code=='access_denied'


def test_lock_revokes_issued_requests_and_survives_restart(service):
    request=service.request()
    assert not service.status()['locked']
    service.enable_lock('Synthetic-Lock-Password-2026')
    with pytest.raises(AssistantError):
        service.authority.require(request,administrator=True)
    request=service.request()
    service.lock()
    with pytest.raises(AssistantError):
        service.authority.require(request,administrator=True)
    assert call(service,'config.read',{'kind':'persona'})['error']=='desktop_locked'
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid)
    assert reopened.status()['locked']
    reopened.unlock('Synthetic-Lock-Password-2026')
    assert not reopened.status()['locked']
    assert call(reopened,'lock.disable',{'password':'wrong-password-2026'})['error']=='desktop_unlock_denied'
    assert reopened.status()['lock_enabled']
    reopened.disable_lock('Synthetic-Lock-Password-2026')
    assert not DesktopManagement(service.store,service.installation,service.windows_sid).status()['locked']


def test_unlock_budget_is_durable_and_not_extended_by_restart(service):
    now=[1000.0]
    service.clock=lambda:now[0]
    service.enable_lock('Synthetic-Lock-Password-2026')
    service.lock()
    for _ in range(5):
        reopened=DesktopManagement(service.store,service.installation,service.windows_sid,clock=lambda:now[0])
        assert call(reopened,'session.unlock',{'password':'wrong-password-2026'})['error']=='desktop_unlock_denied'
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid,clock=lambda:now[0])
    assert call(reopened,'session.unlock',{'password':'Synthetic-Lock-Password-2026'})['error']=='desktop_unlock_limited'
    now[0]+=301
    assert call(reopened,'session.unlock',{'password':'Synthetic-Lock-Password-2026'})['ok']


@pytest.mark.parametrize('op,args,extra',[
    ('config.read',{'kind':'persona'},{'administrator':True}),
    ('config.read',{'kind':'persona','actor':'trusted'},{}),
    ('config.read',{'kind':{}},{}),
    ('config.patch',{'kind':'persona','changes':[],'expected':'x'},{}),
    ('shell.execute',{'command':'noop'},{}),
])
def test_untrusted_flags_and_invalid_operations_denied_and_audited(service,op,args,extra):
    assert not call(service,op,args,**extra)['ok']
    with service.store.db() as db:
        row=db.execute("SELECT count(*) FROM governance_audit WHERE operation='desktop_request_denied'").fetchone()
        assert row[0]==1


def test_transactional_deduplication_and_version_conflict(service):
    old=call(service,'config.read',{'kind':'preferences'})['result']
    args={'kind':'preferences','changes':{'topics':['new-topic']},'expected':old['version']}
    key=uuid.uuid4().hex
    result=call(service,'config.patch',args,key)
    assert result['ok']
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid)
    assert call(reopened,'config.patch',args,key)==result
    assert call(reopened,'config.patch',{**args,'changes':{'topics':['different']}},key)['error']=='request_id_conflict'
    assert call(reopened,'config.patch',args)['error']=='edit_conflict'
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='config'").fetchone()[0]==1


def test_receipt_and_config_rollback_together(service,monkeypatch):
    old=service.store.read('preferences')
    original=service.store.set_state
    def fail(key,value,db=None):
        if key.startswith('desktop-request:'):
            raise AssistantError('injected','test interruption')
        return original(key,value,db)
    monkeypatch.setattr(service.store,'set_state',fail)
    result=call(service,'config.patch',{'kind':'preferences','changes':{'topics':['rollback']},'expected':old['version']})
    assert result['error']=='injected'
    assert service.store.read('preferences')==old


def test_owner_and_password_verifier_not_plaintext(service):
    password='Synthetic-Lock-Password-2026'
    service.enable_lock(password)
    with service.store.db() as db:
        rows=db.execute('SELECT value FROM state WHERE key IN (?,?)',(OWNER,LOCK)).fetchall()
    for row in rows:
        assert password not in row[0] and service.windows_sid not in row[0]
        assert base64.b64encode(password.encode()).decode() not in row[0]


def test_windows_recovery_preserves_data_revokes_old_password_and_context(service):
    old='Original-Application-Password'
    new='Replacement-Application-Password'
    service.enable_lock(old)
    prior=service.request()
    before={kind:service.store.read(kind) for kind in service.store.profile.config}
    owner=service.store.state(OWNER)
    challenge=service.begin_recovery(new)['challenge']
    status=service.finish_recovery(challenge)
    assert status['lock_enabled'] and not status['locked']
    assert service.store.state(OWNER)==owner
    assert {kind:service.store.read(kind) for kind in service.store.profile.config}==before
    with pytest.raises(AssistantError):
        service.authority.require(prior,administrator=True)
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid)
    assert reopened.status()['locked']
    assert call(reopened,'session.unlock',{'password':old})['error']=='desktop_unlock_denied'
    assert call(reopened,'session.unlock',{'password':new})['ok']
    with pytest.raises(AssistantError):
        service.finish_recovery(challenge)
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='desktop_lock_recovered_windows'").fetchone()[0]==1


@pytest.mark.parametrize('scenario',['cancel','expire','restart','wrong_challenge','changed_revision'])
def test_recovery_challenge_cannot_outlive_verification(service,scenario):
    now=[1000.0]
    service.clock=lambda:now[0]
    service.enable_lock('Original-Application-Password')
    challenge=service.begin_recovery('Replacement-Application-Password')['challenge']
    if scenario=='cancel':
        assert service.recovery_dispatch({'action':'desktop-recovery','operation':'cancel','arguments':{}})['ok']
    elif scenario=='expire':
        now[0]+=181
    elif scenario=='restart':
        service=DesktopManagement(service.store,service.installation,service.windows_sid)
    elif scenario=='wrong_challenge':
        challenge='invalid'
    elif scenario=='changed_revision':
        service.lock()
    before=service.store.state(LOCK)
    with pytest.raises(AssistantError):
        service.finish_recovery(challenge)
    assert service.store.state(LOCK)==before
    service.unlock('Original-Application-Password')


def test_recovery_not_registered_in_page_management_protocol(service):
    service.enable_lock('Original-Application-Password')
    before=service.store.state(LOCK)
    for op in ['desktop-recovery','lock.recover','recovery.complete']:
        assert not call(service,op,{'verified':True,'password':'Replacement-Application-Password'})['ok']
    assert not service.dispatch({'action':'desktop-recovery','operation':'complete','arguments':{'challenge':'forged'}})['ok']
    assert service.store.state(LOCK)==before


def test_recovery_transaction_failure_keeps_old_lock_and_consumes_challenge(service,monkeypatch):
    service.enable_lock('Original-Application-Password')
    before=service.store.state(LOCK)
    challenge=service.begin_recovery('Replacement-Application-Password')['challenge']
    original=service.repository.audit
    def fail(actor,operation,*args,**kwargs):
        if operation=='desktop_lock_recovered_windows':
            raise AssistantError('injected','test interruption')
        return original(actor,operation,*args,**kwargs)
    monkeypatch.setattr(service.repository,'audit',fail)
    with pytest.raises(AssistantError,match='test interruption'):
        service.finish_recovery(challenge)
    assert service.store.state(LOCK)==before
    with pytest.raises(AssistantError) as error:
        service.finish_recovery(challenge)
    assert error.value.code=='desktop_recovery_expired'
