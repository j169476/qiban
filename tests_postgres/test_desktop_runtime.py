"""Real PostgreSQL lifecycle tests; synthetic adapters never contact a platform."""
import asyncio
import base64
import json
import subprocess
import sys
import threading
import time

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_runtime import ROLE, DesktopRuntime
from tests_postgres.test_desktop_management import call, seed_accounts
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


class Adapter:
    platform,account='qq','synthetic-account'

    def __init__(self):
        self.stopped=[]

    def capabilities(self):
        return []

    def runtime_stopped(self,generation):
        self.stopped.append(generation)


def account_runtime(service, tmp_path, model):
    from qiban.desktop_siwc import KEY
    from qiban.desktop_siwc_accounts import DesktopSiwcAccounts

    book=seed_accounts(service.store)
    for account in book['accounts'].values():
        account['session']['expires_at']=10000
    service.store.set_state(KEY,book)
    service.codex=DesktopSiwcAccounts(tmp_path/'accounts',service.store,clock=lambda:1000,
        transport=lambda *args: (_ for _ in ()).throw(AssertionError('No network')))
    runtime=DesktopRuntime(service,'switch-test-app',model=model,adapters=[Adapter()])
    return runtime,book,list(book['accounts'])[1]


async def test_unattended_renewal_recovers_cold_autorun_and_respects_manual_stop(service, tmp_path):
    from qiban.desktop_model import DesktopModel
    from qiban.desktop_siwc import KEY

    runtime, book, _ = account_runtime(service, tmp_path, object())
    runtime.model = DesktopModel(service.codex, executable='unused')
    book['accounts'][book['active']]['session']['expires_at'] = 999
    service.store.set_state(KEY, book)
    exchanges = []
    def exchange(*args):
        exchanges.append(1)
        return {'token_type':'Bearer', 'expires_in':3600, 'access_token':'new', 'refresh_token':'rotated'}

    service.codex.transport = exchange
    await runtime.maintain_model_account()
    assert not exchanges
    runtime.preference(service.request(), 'qq', True, runtime.status()['preference_revision'])
    assert runtime.reconcile()['qq']['reason'] == 'model_unavailable'
    await runtime.maintain_model_account()
    assert exchanges == [1] and not runtime.awaiting_model
    assert service.store.state('core-run:qq')['status'] == 'active'
    service.codex.clock = lambda:4100
    await runtime.maintain_model_account()
    assert exchanges == [1, 1]
    runtime.control(service.request(), 'qq', 'stop')
    service.codex.clock = lambda:7200
    await runtime.maintain_model_account()
    assert exchanges == [1, 1]


async def test_account_switch_stops_real_runtime_tasks_before_commit_and_takes_over(service,tmp_path):
    from qiban.desktop_siwc import KEY

    entered=asyncio.Event()
    calls=[]
    class Model:
        async def generate(self,*args,**kwargs):
            selected=service.store.state(KEY)['active']
            calls.append(('started',selected))
            if selected==book['active']:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    # The saved account must remain old until cleanup has finished.
                    assert service.store.state(KEY)['active']==selected
                    calls.append(('closed',selected))
            kwargs['guard']()
            return {'account':selected}
    runtime,book,target=account_runtime(service,tmp_path,Model())
    before=runtime.start('qq')['run']
    task=asyncio.create_task(runtime.cores['qq'].model('synthetic',{},{}))
    await asyncio.wait_for(entered.wait(),2)
    assert call(service,'model.select',{'account_id':target,'expected':1})['ok']
    await asyncio.to_thread(service.codex.thread.join,5)
    assert not service.codex.thread.is_alive()
    assert service.codex.status()['active_account']==target
    assert await asyncio.wait_for(task,2)=={'account':target}
    assert calls==[('started',book['active']),('closed',book['active']),('started',target)]
    assert runtime.cores['qq'].guard()['generation']!=before['generation']
    assert runtime.cores['qq'].platforms.all()[0].stopped==[before['generation']]


def test_switch_preserves_confirmed_and_uncertain_sends_and_explicit_stop(service,tmp_path):
    runtime,book,target=account_runtime(service,tmp_path,object())
    run=runtime.start('qq')['run']
    with service.store.db() as db:
        for status in ('prepared','dispatching','confirmed'):
            db.execute('INSERT INTO core_actions(id,dedupe,generation,platform,account,capability,body,status,created) VALUES(?,?,?,?,?,?,?,?,?)',
                (status,status,run['generation'],'qq','test','send',service.store.ledger_encode('core_actions.body',{'synthetic':True}),status,'2026-10-30'))
    service.codex.begin('select',account_id=target,expected=1)
    service.codex.thread.join(5)
    with service.store.db() as db:
        assert dict(db.execute('SELECT id,status FROM core_actions'))=={
            'prepared':'cancelled','dispatching':'uncertain','confirmed':'confirmed'}
    assert runtime.cores['qq'].guard()['generation']!=run['generation']
    runtime.control(service.request(),'qq','stop')
    service.codex.begin('select',account_id=book['active'],expected=2)
    service.codex.thread.join(5)
    assert service.store.state('core-run:qq')['status']=='paused'
    assert next(p for p in runtime.status()['platforms'] if p['platform']=='qq')['held']


def test_switch_commit_failure_keeps_all_tasks_blocked_until_explicit_retry(service,tmp_path,monkeypatch):
    from qiban.desktop_siwc import KEY

    runtime,book,target=account_runtime(service,tmp_path,object())
    runtime.start('qq')
    save=service.store.set_state
    def fail(key,value,db=None):
        if key==KEY:
            raise OSError('Synthetic account commit interruption')
        return save(key,value,db)
    monkeypatch.setattr(service.store,'set_state',fail)
    service.codex.begin('select',account_id=target,expected=1)
    service.codex.thread.join(5)
    assert runtime.tasks.blocked and service.store.state(KEY)['active']==book['active']
    with pytest.raises(AssistantError):
        runtime.start('qq')
    monkeypatch.setattr(service.store,'set_state',save)
    service.codex.begin('select',account_id=target,expected=1)
    service.codex.thread.join(5)
    assert not runtime.tasks.blocked and service.store.state(KEY)['active']==target
    assert runtime.cores['qq'].guard()['status']=='active'


def test_invalid_selection_does_not_stop_work_and_signed_out_target_cannot_resume(service,tmp_path):
    from qiban.desktop_siwc import KEY

    runtime,book,target=account_runtime(service,tmp_path,object())
    run=runtime.start('qq')['run']
    service.codex.begin('select',account_id=target,expected=0)
    service.codex.thread.join(5)
    assert runtime.tasks.epoch==0 and runtime.cores['qq'].guard()==run
    del book['accounts'][target]['session']
    service.store.set_state(KEY,book)
    service.codex.begin('select',account_id=target,expected=1)
    service.codex.thread.join(5)
    assert service.store.state(KEY)['active']==target
    assert service.store.state('core-run:qq')['status']=='paused'
    assert not runtime.model_ready()
    with pytest.raises(AssistantError) as error:
        runtime.start('qq')
    assert error.value.code=='model_unavailable'


@pytest.mark.parametrize('crash_stage',['before_commit','after_commit'])
def test_cutover_process_death_keeps_admission_closed_until_explicit_selection(service,tmp_path,crash_stage):
    from qiban.desktop_runtime import SESSION
    from qiban.desktop_siwc import KEY

    runtime,book,target=account_runtime(service,tmp_path,object())
    script='''import base64,json,os,sys
from companion_core.postgres import PostgresStore
from qiban.desktop_management import DesktopManagement
from qiban.desktop_runtime import DesktopRuntime
from qiban.desktop_siwc_accounts import DesktopSiwcAccounts
v=json.load(sys.stdin)
store=PostgresStore(v['root'],v['options'],base64.b64decode(v['key']))
management=DesktopManagement(store,'installation-test','S-1-5-21-test-owner')
broker=DesktopSiwcAccounts(v['auth'],store,clock=lambda:1000)
management.codex=broker
class Adapter:
    platform,account='qq','synthetic-account'
    def capabilities(self):return []
runtime=DesktopRuntime(management,'cutover-crash-app',model=object(),adapters=[Adapter()])
runtime.start('qq')
change=broker.account_change
def crashing_change(commit,*,ready):
    def crashing_commit():
        if v['stage']=='before_commit':os._exit(94)
        commit()
        os._exit(94)
    return change(crashing_commit,ready=ready)
broker.account_change=crashing_change
broker.selection=(v['target'],1)
broker._operate('select')
'''
    payload={'root':str(service.store.root),'options':service.store.database._options,
        'key':base64.b64encode(service.store._test_key).decode(),'auth':str(tmp_path/'crash-auth'),
        'target':target,'stage':crash_stage}
    child=subprocess.run([sys.executable,'-c',script],input=json.dumps(payload),text=True,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.returncode==94
    checkpoint=service.store.state(SESSION)['account_switch']
    assert checkpoint['status']=='stopping'
    expected_active=book['active'] if crash_stage=='before_commit' else target
    assert service.store.state(KEY)['active']==expected_active
    recovered=DesktopRuntime(service,'new-app-after-power-loss',model=object(),adapters=[Adapter()])
    assert recovered.tasks.blocked
    assert call(service,'model.status')['result']['handoff_pending']
    assert recovered.reconcile()['qq']['reason']=='model_account_switching'
    assert service.store.state('core-run:qq')['status']=='paused'
    service.codex.begin('select',account_id=target,expected=service.store.state(KEY)['revision'])
    service.codex.thread.join(5)
    assert not service.codex.thread.is_alive() and not recovered.tasks.blocked
    assert not call(service,'model.status')['result']['handoff_pending']
    run=recovered.cores['qq'].guard()
    assert run['pending_since']==checkpoint['resume']['qq']['pending_since']
    assert service.store.state(KEY)['active']==target


def test_post_commit_resume_failure_preserves_new_account_and_block(service,tmp_path,monkeypatch):
    from qiban.desktop_runtime import SESSION
    from qiban.desktop_siwc import KEY

    runtime,book,target=account_runtime(service,tmp_path,object())
    runtime.start('qq')
    save=service.store.set_state
    def fail(key,value,db=None):
        if key==SESSION and value.get('account_switch',{}).get('status')=='complete':
            raise OSError('Synthetic resume commit failure')
        return save(key,value,db)
    monkeypatch.setattr(service.store,'set_state',fail)
    service.codex.begin('select',account_id=target,expected=1)
    service.codex.thread.join(5)
    assert service.codex.status()['code']=='model_account_handoff_pending'
    assert service.store.state(KEY)['active']==target and runtime.tasks.blocked
    assert service.store.state('core-run:qq')['status']=='paused'


def test_model_account_operations_require_unlocked_local_admin(service,tmp_path):
    from qiban.desktop_codex import DesktopCodex

    service.codex=DesktopCodex(tmp_path/'account')
    assert call(service,'model.status',{})['result']['state']=='unchecked'
    assert call(service,'model.open',{})['error']=='model_login_expired'
    assert call(service,'model.login',{'url':'https://evil.invalid'})['error']=='management_operation'
    assert call(service,'model.cancel',{})['result']=={'cancel_requested':False}
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='desktop_model_cancel_idle'").fetchone()[0]==1
    call(service,'lock.enable',{'password':'Synthetic-Account-Lock-2026!'})
    call(service,'session.lock',{})
    for operation in ('status','login','logout','refresh','open','cancel'):
        assert not call(service,'model.'+operation,{})['ok']
    assert service.codex.thread is None


def attach(service,application='synthetic-app',model=True):
    return DesktopRuntime(service,application,model=object() if model else None,adapters=[Adapter()])


def wait_runtime(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, 'Desktop scheduler did not reach expected state'
        time.sleep(.01)


def backlog_sources(service):
    from companion_core import events
    from companion_core.contracts import ActorRef, Event

    settings=service.store.read('companion')
    settings['data'].update(reply_context_chars=24000,reply_context_tokens=80000,reply_output_reserve=4096)
    service.store.write('companion',settings['data'],settings['version'],'test')
    source=[Event('qq','synthetic-account',str(index),ActorRef('qq','friend','synthetic-account'),
                  'private:friend',f'2020-01-0{index+1}T00:00:00+00:00',
                  'SYNTHETIC_PRIVATE_BACKLOG 原始消息。'*700) for index in range(3)]
    for event in source:
        events.ingest(service.store,event)
    return source


async def test_backlog_summary_counts_budget_encrypts_cache_and_never_acknowledges_sources(service,tmp_path):
    from companion_core import budget
    calls=[]
    class Model:
        async def generate(self,*args,**kwargs):
            kwargs['guard']()
            calls.append(True)
            return {'text':'SYNTHETIC_SUMMARY 摘要不代表已经回复。'}
    runtime,_,_=account_runtime(service,tmp_path,Model())
    source=backlog_sources(service)
    runtime.start('qq')
    core=runtime.cores['qq']
    options=({'persona':'基础人设','memory':{'records':[],'catalog':[]}},source,'最终回复规则',{'type':'object'})
    context,first=await core.prepare_backlog(*options,guard=lambda:None)
    assert first['summary_calls']==len(calls)>0 and context['backlog']['message_count']==3
    _,second=await core.prepare_backlog(*options,guard=lambda:None)
    assert second['summary_calls']==0 and second['summaries_reused']>0
    used=budget.status(service.store,service.store.read('policy')['data'],core.clock())['used']
    assert used==len(calls)
    with service.store.db() as db:
        raw=[row[0] for row in db.execute("SELECT value FROM state WHERE key LIKE 'backlog-summary:%'")]
        assert raw and all('SYNTHETIC_SUMMARY' not in str(value) and 'SYNTHETIC_PRIVATE_BACKLOG' not in str(value) for value in raw)
        assert db.execute('SELECT count(*) FROM core_actions').fetchone()[0]==0
    assert all(service.store.state('core-processed:'+event.id) is None for event in source)


async def test_backlog_source_retraction_during_summary_prevents_cache_commit(service,tmp_path):
    from companion_core import events
    class Model:
        async def generate(self,*args,**kwargs):
            events.forget(service.store,source[0].id)
            return {'text':'不得提交的摘要'}
    runtime,_,_=account_runtime(service,tmp_path,Model())
    source=backlog_sources(service)
    runtime.start('qq')
    with pytest.raises(AssistantError) as error:
        await runtime.cores['qq'].prepare_backlog({},source,'规则',{'type':'object'},guard=lambda:None)
    assert error.value.code=='context_changed'
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM state WHERE key LIKE 'backlog-summary:%'").fetchone()[0]==0


async def test_group_backlog_hard_cut_precedes_model_and_preserves_originals(service,tmp_path):
    from companion_core import events
    from companion_core.backlog_context import GROUP_CONTEXT_CHARS
    from companion_core.context_budget import encoded
    from companion_core.contracts import ActorRef, Event

    class Model:
        async def generate(self,*args,**kwargs):
            raise AssertionError('Truncated group input fits; no summary call needed')

    runtime,_,_=account_runtime(service,tmp_path,Model())
    settings=service.store.read('companion')
    settings['data'].update(reply_context_chars=24000,reply_context_tokens=80000,reply_output_reserve=4096)
    service.store.write('companion',settings['data'],settings['version'],'test')
    source=[Event('qq','synthetic-account',str(index),ActorRef('qq','friend','synthetic-account'),
                  'group:friends',f'2020-01-01T00:00:{index:02d}+00:00',
                  f'群聊原文{index}：'+'水群消息'*2000,visibility='group') for index in range(40)]
    for event in source:
        events.ingest(service.store,event)
    runtime.start('qq')
    context,report=await runtime.cores['qq'].prepare_backlog(
        {'persona':'基础人设'},source,'规则',{'type':'object'},guard=lambda:None)
    assert len(encoded({'units':context['backlog']['units']})) <= GROUP_CONTEXT_CHARS
    assert report['selection']['omitted_messages']>0 and report['selection']['partial_messages']==1
    assert report['summary_calls']==0
    assert len(report['complete_sources'])<len(report['sources'])<len(source)
    assert all(events.get(service.store,event.id)['event']['text']==event.text for event in source)
    assert all(service.store.state('core-processed:'+event.id) is None for event in source)
    with service.store.db() as db:
        assert db.execute('SELECT count(*) FROM core_actions').fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM state WHERE key LIKE 'reply-window:%'").fetchone()[0]==0


async def test_backlog_account_switch_cancels_summary_and_restarts_on_selected_account(service,tmp_path):
    from qiban.desktop_siwc import KEY
    entered=asyncio.Event()
    accounts=[]
    class Model:
        async def generate(self,*args,**kwargs):
            account=service.store.state(KEY)['active']
            accounts.append(account)
            if account==book['active']:
                entered.set()
                await asyncio.Event().wait()
            kwargs['guard']()
            return {'text':'新账号完成的合成摘要'}
    runtime,book,target=account_runtime(service,tmp_path,Model())
    source=backlog_sources(service)
    runtime.start('qq')
    task=asyncio.create_task(runtime.cores['qq'].prepare_backlog({},source,'规则',{'type':'object'},guard=lambda:None))
    await asyncio.wait_for(entered.wait(),3)
    service.codex.begin('select',account_id=target,expected=1)
    await asyncio.to_thread(service.codex.thread.join,5)
    context,report=await asyncio.wait_for(task,5)
    assert accounts[0]==book['active'] and set(accounts[1:])=={target}
    assert context['backlog']['message_count']==3 and report['summary_calls']==len(accounts)-1


def test_scheduler_stop_waits_for_cleanup_before_restart_and_restore(service):
    runtime = attach(service)
    entered, finishing, release, finished = (threading.Event() for _ in range(4))
    core = runtime.cores['qq']
    async def poll():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finishing.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            finished.set()
    core.poll = poll
    backup = service.backups.create()
    runtime.start('qq')
    runtime.open()
    try:
        assert entered.wait(5)
        assert call(service,'runtime.control',{'platform':'qq','action':'stop'})['ok']
        assert finishing.wait(5)
        assert call(service,'runtime.control',{'platform':'qq','action':'start'})['error']=='worker_busy'
        with pytest.raises(AssistantError) as error:
            service.backups.prepare(backup['name'])
        assert error.value.code=='restore_stopping'
        release.set()
        wait_runtime(lambda:finished.is_set() and not runtime.scheduler.status('qq')['stopping'])
        assert service.backups.prepare(backup['name'])['token']
        core.poll = lambda:asyncio.sleep(0,result=[])
        core.cycle = lambda:asyncio.sleep(0)
        assert call(service,'runtime.control',{'platform':'qq','action':'start'})['ok']
    finally:
        release.set()
        runtime.close()
    assert not runtime.scheduler.thread.is_alive() and not runtime.tasks.attempts


def test_scheduler_failure_records_fixed_notice_and_manual_stop_intent(service):
    runtime = attach(service)
    core = runtime.cores['qq']
    calls=[]
    async def poll():
        calls.append(True)
        raise AssistantError('model_usage_limit','SYNTHETIC_PRIVATE_FAILURE')
    core.poll=poll
    runtime.preference(service.request(),'qq',True,runtime.status()['preference_revision'])
    runtime.start('qq')
    runtime.open()
    try:
        wait_runtime(lambda:service.store.state('core-run:qq')['status']=='paused')
        assert runtime.start('qq',automatic=True)['reason']=='explicit_stop'
        notices=call(service,'notifications.list')['result']['items']
        assert notices[0]['code']=='model_usage_limit'
        assert 'SYNTHETIC_PRIVATE_FAILURE' not in json.dumps(notices)
        assert calls==[True]
    finally:
        runtime.close()


def test_late_old_failure_cannot_pause_replacement_generation(service):
    runtime=attach(service)
    old=runtime.start('qq')['run']
    runtime.control(service.request(),'qq','stop')
    new=runtime.start('qq')['run']
    runtime.fail('qq','model_auth',old['generation'])
    assert runtime.cores['qq'].guard()['generation']==new['generation']
    assert not call(service,'notifications.list')['result']['items']


def test_scheduler_thread_participates_in_account_cutover(service,tmp_path):
    runtime,book,target=account_runtime(service,tmp_path,object())
    entered=threading.Event()
    generations=[]
    core=runtime.cores['qq']
    async def poll():
        generation=core.guard()['generation']
        generations.append(generation)
        if len(generations)==1:
            entered.set()
            await asyncio.Event().wait()
        return []
    core.poll=poll
    core.cycle=lambda:asyncio.sleep(0)
    runtime.start('qq')
    runtime.open()
    try:
        assert entered.wait(5)
        assert call(service,'model.select',{'account_id':target,'expected':1})['ok']
        service.codex.thread.join(5)
        assert not service.codex.thread.is_alive()
        wait_runtime(lambda:len(generations)>=2)
        assert generations[0]!=generations[1]
        assert service.codex.status()['active_account']==target
        assert not runtime.tasks.blocked
    finally:
        runtime.close()


def test_runtime_has_own_role_and_run_id_without_codex_chat(service,monkeypatch):
    monkeypatch.delenv('CODEX_THREAD_ID',raising=False)
    runtime=attach(service)
    assert runtime.role.key!=service.actor.key
    result=call(service,'runtime.control',{'platform':'qq','action':'start'})
    assert result['ok'],result
    run=result['result']['run']
    assert run['owner'].startswith('qiban-run:') and run['generation']
    assert runtime.cores['qq'].guard()['generation']==run['generation']
    assert service.store.state('service-admin-thread') is None
    with service.store.db() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0


def test_stop_retains_preference_and_blocks_auto_after_same_app_worker_restart(service):
    runtime=attach(service)
    status=runtime.status()
    assert call(service,'runtime.preference',{'platform':'qq','enabled':True,'expected':status['preference_revision']})['ok']
    run=call(service,'runtime.control',{'platform':'qq','action':'start'})['result']['run']
    assert call(service,'runtime.control',{'platform':'qq','action':'stop'})['ok']
    assert runtime.cores['qq'].platforms.all()[0].stopped==[run['generation']]
    assert runtime.start('qq',automatic=True)=={'started':False,'reason':'explicit_stop'}
    restarted=attach(service)
    assert restarted.start('qq',automatic=True)=={'started':False,'reason':'explicit_stop'}
    assert next(p for p in restarted.status()['platforms'] if p['platform']=='qq')['autorun']
    fresh_application=attach(service,'new-application-session')
    assert fresh_application.start('qq',automatic=True)['started']


def test_model_unavailable_does_not_clear_manual_stop_or_start_role(service):
    runtime=attach(service,model=False)
    call(service,'runtime.control',{'platform':'qq','action':'stop'})
    assert call(service,'runtime.control',{'platform':'qq','action':'start'})['error']=='model_unavailable'
    assert runtime.start('qq',automatic=True)['reason']=='explicit_stop'
    assert service.store.state('core-run:qq',{}).get('status')!='active'


def test_preferences_use_cas_and_never_start_platform_implicitly(service):
    runtime=attach(service)
    revision=runtime.status()['preference_revision']
    args={'platform':'qq','enabled':True,'expected':revision}
    assert call(service,'runtime.preference',args)['ok']
    assert call(service,'runtime.preference',args)['error']=='edit_conflict'
    assert service.store.state('core-run:qq') is None
    assert call(service,'runtime.preference',{**args,'enabled':1})['error']=='runtime_request'


def test_locking_management_does_not_revoke_running_role(service):
    runtime=attach(service)
    run=call(service,'runtime.control',{'platform':'qq','action':'start'})['result']['run']
    service.enable_lock('Synthetic-Management-Lock-2026!')
    service.lock()
    assert call(service,'runtime.control',{'platform':'qq','action':'stop'})['error']=='desktop_locked'
    assert runtime.cores['qq'].guard(run['generation'])['status']=='active'
    request=service.authority.issue(actor=runtime.role,conversation='role-only',audience='private',recipients=(runtime.role,),purpose='reply')
    with pytest.raises(AssistantError):
        runtime.control(request,'qq','stop')


def test_old_runtime_cannot_stop_or_drive_new_run(service):
    old=attach(service)
    old.start('qq')
    new=attach(service)
    new_run=new.start('qq')['run']
    for work in (lambda:old.cores['qq'].guard(),lambda:old.cores['qq'].stop(),old.close):
        with pytest.raises(AssistantError) as error:
            work()
        assert error.value.code=='stale_runtime'
    assert new.cores['qq'].guard()['generation']==new_run['generation']


def test_restart_classifies_unfinished_actions_without_resending(service):
    runtime=attach(service)
    run=runtime.start('qq')['run']
    with service.store.db() as db:
        for status in ('prepared','dispatching','confirmed'):
            db.execute('INSERT INTO core_actions(id,dedupe,generation,platform,account,capability,body,status,created) VALUES(?,?,?,?,?,?,?,?,?)',
                       (status,status,run['generation'],'qq','test','send',service.store.ledger_encode('core_actions.body',{'synthetic':True}),status,'2026-10-30'))
    restarted=attach(service)
    assert next(p for p in restarted.status()['platforms'] if p['platform']=='qq')['status']=='paused'
    with service.store.db() as db:
        assert dict(db.execute('SELECT id,status FROM core_actions'))=={'prepared':'cancelled','dispatching':'uncertain','confirmed':'confirmed'}


def test_close_holds_current_app_but_keeps_durable_role(service):
    runtime=attach(service)
    role=service.store.state(ROLE)
    runtime.start('qq')
    runtime.close()
    restarted=attach(service)
    assert restarted.start('qq',automatic=True)['reason']=='explicit_stop'
    assert service.store.state(ROLE)==role


def test_restore_preserves_current_runtime_and_holds_autorun(service):
    runtime=attach(service)
    backup=service.backups.create()
    runtime.preference(service.request(),'qq',True,runtime.status()['preference_revision'])
    preview=service.backups.prepare(backup['name'])
    challenge=service.backups.begin(preview['token'])['challenge']
    service.backups.complete(challenge)
    assert runtime.status()['runtime_instance']==runtime.instance
    assert runtime.start('qq',automatic=True)['reason']=='explicit_stop'


def test_autorun_does_not_follow_a_changed_account(service):
    runtime=attach(service)
    runtime.preference(service.request(),'qq',True,runtime.status()['preference_revision'])
    adapter=Adapter()
    adapter.account='different-account'
    changed=DesktopRuntime(service,'new-application',model=object(),adapters=[adapter])
    assert changed.reconcile()['qq']['reason']=='account_changed'
    assert service.store.state('core-run:qq',{}).get('status')!='active'


def test_notifications_are_durable_deduplicated_and_do_not_use_chat(service):
    runtime=attach(service,model=False)
    runtime.preference(service.request(),'qq',True,runtime.status()['preference_revision'])
    runtime.reconcile()
    runtime.reconcile()
    items=call(service,'notifications.list')['result']['items']
    assert len(items)==1 and items[0]['code']=='model_unavailable'
    assert not items[0]['read']
    assert call(service,'notifications.read',{'identifier':items[0]['id']})['ok']
    assert call(service,'notifications.list')['result']['unread']==0
    assert service.store.state('service-admin-thread') is None
    with service.store.db() as db:
        values=[row[0] for row in db.execute("SELECT value FROM state WHERE key LIKE 'desktop-notice:%'")]
    assert all('模型' not in str(value) and 'model_unavailable' not in str(value) for value in values)


def test_reply_configurations_preserve_documents_and_scoped_reads(desktop_store):
    from companion_core.storage import REPLY_CONFIGURATIONS, read_reply_configurations

    expected = {kind: desktop_store.read(kind) for kind in REPLY_CONFIGURATIONS}
    assert read_reply_configurations(desktop_store) == expected

    class Scoped:
        def __getattr__(self, name):
            return getattr(desktop_store, name)

        def read(self, kind):
            return {'version': 'scoped', 'data': kind}

    assert all(item['version'] == 'scoped' for item in read_reply_configurations(Scoped()).values())


def test_notification_pages_upgrade_and_bulk_edits(service):
    notices = service.notifications
    # Seed the old on-disk format, including an item older than the legacy limit.
    with service.store.db() as db:
        for i in range(201):
            key = f'{i:03}'
            service.store.set_state('desktop-notice:' + key, {'id': key, 'read': i == 0}, db)
    def page(number):
        return call(service, 'notifications.page', {'page': number})['result']
    def update(action, ids):
        return call(service, 'notifications.update', {'action': action, 'identifiers': ids})['result']['changed']
    assert [item['id'] for item in page(21)['items']] == ['000']
    assert page(1)['unread'] == 200
    new_id = notices.record('network_error', 'once', 'qq')
    assert update('read', [new_id, new_id, '200']) == 2
    assert update('read', [new_id, '200']) == 0
    assert update('delete', [new_id, '000', '199']) == 3
    assert update('delete', [new_id]) == 0
    assert notices.record('network_error', 'once', 'qq') == new_id
    last = page(99)
    assert (last['page'], last['pages'], last['total'], last['unread']) == (20, 20, 199, 198)
    assert not call(service, 'notifications.update', {'action': 'delete', 'identifiers': ['1', 2]})['ok']


def test_notifications_require_unlocked_management(service):
    service.notifications.record('untrusted raw exception text','synthetic','qq')
    item=call(service,'notifications.list')['result']['items'][0]
    assert item['code']=='runtime_failed' and 'untrusted' not in item['message']
    service.enable_lock('Synthetic-Notification-Lock!')
    service.lock()
    assert call(service,'notifications.page',{'page':1})['error']=='desktop_locked'
    assert call(service,'notifications.update',{'action':'delete','identifiers':[item['id']]})['error']=='desktop_locked'
    assert call(service,'notifications.list')['error']=='desktop_locked'
    assert call(service,'notifications.read',{'identifier':item['id']})['error']=='desktop_locked'


def test_unavailable_platform_cannot_be_enabled_for_autorun(service):
    runtime=DesktopRuntime(service,'synthetic-application')
    assert call(service,'runtime.preference',{'platform':'qq','enabled':True,'expected':runtime.status()['preference_revision']})['error']=='platform_unavailable'
    assert not next(p for p in runtime.status()['platforms'] if p['platform']=='qq')['autorun']


@pytest.fixture
def backlog_permission(desktop_store):
    from companion_core import events
    from companion_core.contracts import ActorRef, Event
    from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
    from companion_core.reply_policy import Participation

    scope=ReplyScope('qq','synthetic-account','group','friends')
    ReplyAuthorizations(desktop_store).update(scope,enabled=True,expected_version=None,actor='test')
    event=Event('qq','synthetic-account','old-mention',ActorRef('qq','friend','synthetic-account'),
                'group:friends','2020-01-01T00:00:00+00:00','过去的问题',visibility='group',
                metadata={'qq':{'target_id':'friends','trigger':'mention','identity_quality':'verified'}})
    events.ingest(desktop_store,event)
    return desktop_store,Participation(desktop_store),scope,event


def test_backlog_permission_is_once_only_and_does_not_wake_live_group(backlog_permission):
    store,policy,scope,event=backlog_permission
    ticket=policy.admit_backlog(scope,event.id,'run','frozen-batch')
    assert ticket and policy.context(ticket,'run')['mode']=='group_backlog'
    assert store.state(policy.window_key(scope)) is None
    assert policy.admit(scope,'ordinary','none','run') is None
    policy.reserve(ticket,'run','catchup-action')
    with pytest.raises(AssistantError):
        policy.reserve(ticket,'run','second-action')
    policy.settle('catchup-action','succeeded')
    assert policy.admit_backlog(scope,event.id,'new-run','different-batch') is None
    assert store.state(policy.window_key(scope)) is None
    assert policy.admit(scope,'new-mention','mention','new-run')


@pytest.mark.parametrize('status',['reserved','uncertain'])
def test_backlog_reserved_or_uncertain_send_is_not_replayed_after_restart(backlog_permission,status):
    from companion_core.reply_policy import Participation
    store,policy,scope,event=backlog_permission
    ticket=policy.admit_backlog(scope,event.id,'run','batch')
    policy.reserve(ticket,'run','catchup-action')
    if status=='uncertain':
        policy.settle('catchup-action',status)
    restarted=Participation(store)
    assert restarted.admit_backlog(scope,event.id,'new-run','new-batch') is None
    assert restarted.admit(scope,'ordinary','none','new-run') is None


def test_backlog_silent_end_does_not_close_or_extend_live_window(backlog_permission):
    store,policy,scope,event=backlog_permission
    live=policy.admit(scope,'live-mention','mention','run')
    before=store.state(policy.window_key(scope))
    ticket=policy.admit_backlog(scope,event.id,'run','batch')
    policy.end(ticket,'run')
    assert store.state(policy.window_key(scope))==before
    assert policy.eligible(live,'run')
    assert policy.admit_backlog(scope,event.id,'new-run','different-batch') is None


def test_backlog_pre_submit_failure_can_resume_same_batch_on_new_run(backlog_permission):
    store,policy,scope,event=backlog_permission
    ticket=policy.admit_backlog(scope,event.id,'old-run','batch')
    policy.reserve(ticket,'old-run','before-submit')
    policy.settle('before-submit','cancelled')
    renewed=policy.admit_backlog(scope,event.id,'new-run','batch')
    assert renewed and policy.eligible(renewed,'new-run')
    assert not policy.eligible(ticket,'old-run')
    with pytest.raises(AssistantError) as error:
        policy.admit_backlog(scope,event.id,'new-run','changed-batch')
    assert error.value.code=='context_changed'


@pytest.mark.parametrize('change',['source','permission'])
def test_backlog_source_or_permission_change_blocks_send(backlog_permission,change):
    from dataclasses import replace

    from companion_core import events
    from companion_core.reply_authorization import ReplyAuthorizations
    store,policy,scope,event=backlog_permission
    ticket=policy.admit_backlog(scope,event.id,'run','batch')
    if change=='source':
        events.ingest(store,replace(event,text='已更正的问题'))
    else:
        permissions=ReplyAuthorizations(store)
        permissions.update(scope,enabled=False,expected_version=permissions.version(scope),actor='test')
    with pytest.raises(AssistantError):
        policy.reserve(ticket,'run','must-not-send')
    assert store.state('reply-send:must-not-send') is None


@pytest.mark.parametrize('change',['text-only','other-target'])
def test_backlog_admission_requires_canonical_verified_trigger(backlog_permission,change):
    from dataclasses import replace

    from companion_core import events
    store,policy,scope,event=backlog_permission
    metadata={'qq':{**event.metadata['qq']}}
    if change=='text-only':
        metadata['qq']['trigger']='none'
    else:
        metadata['qq']['target_id']='other'
    events.ingest(store,replace(event,metadata=metadata,text='@栖伴'))
    with pytest.raises(AssistantError) as error:
        policy.admit_backlog(scope,event.id,'run','batch')
    assert error.value.code=='reply_event_untrusted'


def test_model_exit_is_reported_as_failure_instead_of_user_stop(service):
    runtime=attach(service)
    run=runtime.start('qq')['run']
    runtime.fail('qq','model_process_exit',run['generation'])
    status=next(p for p in runtime.status()['platforms'] if p['platform']=='qq')
    assert status['failed'] and status['held']
    assert status['reason']=='model_process_exit' and '模型' in status['message']
    runtime.start('qq')
    runtime.control(service.request(),'qq','stop')
    status=next(p for p in runtime.status()['platforms'] if p['platform']=='qq')
    assert not status['failed'] and status['message']==''
