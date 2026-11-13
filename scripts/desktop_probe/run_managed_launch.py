"""Check the assembled EXE's automatic first launch, with no managed-mode flag."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))

from credential_vault import VaultCredentials
from run_lifecycle import api
from run_storage import ciphertext
from storage_worker import VAULT

from companion_core.process_scope import ProcessScope


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity-check',action='store_true')
    parser.add_argument('--room-cancel-check',action='store_true')
    parser.add_argument('--bili-qr-check',action='store_true')
    parser.add_argument('--bili-policy-check',action='store_true')
    parser.add_argument('--fact-check',action='store_true')
    parser.add_argument('--growth-check',action='store_true')
    parser.add_argument('--documents-check',action='store_true')
    parser.add_argument('--snapshot-check',action='store_true')
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    exe,evidence=args.exe.resolve(strict=True),args.evidence.resolve()
    evidence.mkdir(parents=True,exist_ok=False)
    profile=evidence/'中文用户'
    root=profile/'data/installation'
    scope=ProcessScope()
    vault=VaultCredentials()
    report={'passed':False}
    handles={}
    env={**os.environ,'PATH':str(Path(os.environ['SystemRoot'])/'System32'),'PYTHONHOME':str(evidence/'no-python')}
    before={p.relative_to(exe.parent).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in exe.parent.rglob('*') if p.is_file()}
    try:
        process=subprocess.Popen([str(exe),'--data-dir',str(profile)],cwd=evidence,env=env,
            stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
        scope.attach(process)
        scope.resume()
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            log=profile/'logs/events.jsonl'
            events=[]
            if log.exists():
                for line in log.read_text(encoding='utf8').splitlines():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # A log write may be in progress.
            if any(e['name']=='component_state' and e['detail']['state']=='ready' for e in events):
                break
            if process.poll() is not None or any(e['name']=='component_state' and e['detail']['state']=='failed' for e in events):
                raise AssertionError('Managed first launch failed')
            time.sleep(.1)
        else:
            raise TimeoutError('Managed first launch deadline')
        state=read(root/'installation.json')
        assert state['phase']=='ready'
        credential=json.loads(vault.get_password(VAULT,state['installation']))
        config=read(root/'storage-config.json')
        options={**config['database'],'password':credential['database_password']}
        if args.fact_check:
            from datetime import datetime, timezone

            from storage_worker import open_store

            from companion_core import events
            from companion_core.contracts import ActorRef, Event
            from companion_core.governance import GovernedContext
            from companion_core.temporal_facts import PersonalFacts

            store = open_store(root/'storage-config.json')
            try:
                for index,city in enumerate(('合成事实141甲城','合成事实141乙城')):
                    event = Event('fixture','141',str(index),ActorRef('fixture','person'),'dm',
                                  datetime.now(timezone.utc).isoformat(),'我住'+city)
                    events.ingest(store,event)
                    context = GovernedContext(store,event)
                    context.classify_event()
                    facts = PersonalFacts(context,event,lambda: datetime.now(timezone.utc))
                    facts.context()
                    context.freeze()
                    assert facts.save([{'attribute':'residence','value':city,'quote':event.text,
                                        'valid_from':None,'supersedes':[]}])['written']==1
            finally:
                store.database.close()
        if args.snapshot_check:
            from datetime import datetime, timedelta, timezone

            from storage_worker import open_store

            from companion_core import events, fact_retirement
            from companion_core.contracts import ActorRef, Event
            from companion_core.governance import GovernedContext
            from companion_core.temporal_facts import PersonalFacts

            store = open_store(root/'storage-config.json')
            try:
                with store.lock, store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    for scenario in ('unused', 'kept'):
                        old = datetime.now(timezone.utc) - timedelta(days=110)
                        event = Event('fixture', '149', scenario, ActorRef('fixture', scenario), 'dm',
                                      old.isoformat(), '我住合成测试城')
                        events.ingest(store, event)
                        context = GovernedContext(store, event)
                        context.classify_event()
                        identifier = 'fact-snapshot-' + scenario + '-149'
                        data = {'schema_version': 1, 'id': identifier, 'kind': 'user_preference',
                            'content': event.text, 'confidence': 'explicit', 'tags': [], 'locked': False,
                            'source': {'type': 'interaction', 'recorded_at': old.isoformat(), 'reference': event.id},
                            'fact': {'actor': event.actor.key, 'account': event.account, 'conversation': event.thread,
                                'attribute': 'residence', 'value': '合成测试城', 'quote': event.text,
                                'valid_from': old.isoformat(), 'supersedes': []}}
                        store.write('memories', data, None, 'window-fixture', identifier)
                        context.classify_written('memories', identifier, personal=True, sources=['event/' + event.id])
                        PersonalFacts(context, event, lambda: datetime.now(timezone.utc)).freeze_projection()
                        view = next(r for r in context.resources if r.startswith('personal-fact-view/'))
                        fact_retirement.retire(store, identifier, fact_retirement.preview(store, identifier)['fingerprint'],
                            'window-fixture', clock=lambda: datetime.now(timezone.utc) - timedelta(days=100))
                        if scenario == 'kept':
                            store.set_state('reply-trace:fixture-149', {'sources': [], 'retention_roots': [view]}, db)
            finally:
                store.database.close()
        if args.documents_check:
            from storage_worker import open_store
            store = open_store(root/'storage-config.json')
            try:
                for kind in ('knowledge', 'diaries'):
                    data = {'schema_version': 1, 'id': 'fixture-document-147', 'content': '合成资料147中文123🙂',
                            'tags': ['合成标签'], 'locked': True,
                            'source': {'type': 'user_instruction', 'recorded_at': '2026-11-07T00:00:00Z',
                                       'reference': '合成来源147'}}
                    if kind == 'knowledge':
                        data.update(coverage={'kind': 'web', 'bvid': '', 'start_seconds': 0, 'end_seconds': 0,
                                              'duration_seconds': 0, 'complete': False}, disagrees_with=[])
                    else:
                        data['memory_refs'] = [{'id': 'synthetic-memory-147', 'version': 'a' * 64}]
                    store.write(kind, data, None, 'window-fixture', data['id'])
            finally:
                store.database.close()
        if args.growth_check:
            from storage_worker import open_store
            store = open_store(root/'storage-config.json')
            try:
                data = {**store.profile.defaults['growth'], 'mood': '归档旧状态145中文123🙂'}
                store.set_state('public-growth', {'data': data, 'version': 'fixture-invalid-145'})
            finally:
                store.database.close()
        if args.identity_check:
            from storage_worker import open_store

            from companion_core.access import RequestAuthority
            from companion_core.contracts import ActorRef
            from companion_core.governance import GovernanceRepository
            from companion_core.relations import RelationService

            store = open_store(root/'storage-config.json')
            try:
                registry = RelationService(GovernanceRepository(store), RequestAuthority())
                registry.register_actor(ActorRef("qq","fixture-qq-114","fixture"))
                registry.register_actor(ActorRef("wechat","fixture-wx-114","fixture"))
            finally:
                store.database.close()
        if args.room_cancel_check:
            from types import SimpleNamespace
            from uuid import uuid4

            from install_windows import user_sid
            from storage_worker import open_store

            from qiban.desktop_management import DesktopManagement

            store = open_store(root/'storage-config.json')
            try:
                manager = DesktopManagement(store,state['installation'],user_sid())
                manager.room.core = SimpleNamespace(guard=lambda: {"generation":"window-fixture","read_only":False})
                assert manager.room.post(manager.session_id,"待取消的窗口测试消息",uuid4().hex)["new"]
            finally:
                store.database.close()
        if args.bili_policy_check:
            from bili_assistant.browser_login_transport import crypt
            from companion_core.storage import atomic_write

            binding = ("qiban.desktop.bilibili:"+state["installation"]).encode()
            account = {"uid":"136","name":"配额测试","revision":"fixture","cookies":{}}
            atomic_write(root/"bili-account.sealed",crypt(json.dumps(account).encode(),binding))
        digest=ciphertext(options)
        assert read(profile/'data/application-session.json')['Stage'] in {'running','verified'}
        scope._capture_members(handles)
        assert len(handles)>2
        process.kill()
        process.wait(timeout=10)
        assert all(api.WaitForSingleObject(handle,10000)==0 for handle in handles.values())
        report['automatic_first_launch']=True
        report['host_death_owned_handles_exited']=len(handles)
        scope.close()
        for handle in handles.values():
            api.CloseHandle(handle)
        handles={}
        # Existing-state recovery is also exercised in the full real-window checks.
        scope=ProcessScope()
        process=subprocess.Popen([str(exe),'--smoke',*(['--identity-check'] if args.identity_check else []),
            *(['--fact-check'] if args.fact_check else []),
            *(['--growth-check'] if args.growth_check else []),
            *(['--documents-check'] if args.documents_check else []),
            *(['--snapshot-check'] if args.snapshot_check else []),
            *(['--room-cancel-check'] if args.room_cancel_check else []),
            *(['--bili-qr-check'] if args.bili_qr_check else []),
            *(['--bili-policy-check'] if args.bili_policy_check else []),'--data-dir',str(profile)],cwd=evidence,env=env,
            stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
        scope.attach(process)
        scope.resume()
        assert process.wait(timeout=180 if args.snapshot_check else 120)==0
        result=read(profile/'result.json')
        assert result['passed']
        assert read(root/'installation.json')['installation']==state['installation']
        assert json.loads(vault.get_password(VAULT,state['installation']))==credential
        report['window_checks']=[e['name'] for e in result['events'] if e['name'].startswith('check_')]
        if args.identity_check:
            assert 'check_manual_identity_confirmation_pass' in report['window_checks']
        if args.room_cancel_check:
            assert 'check_room_cancel_preserves_original_pass' in report['window_checks']
        if args.bili_qr_check:
            assert 'check_bili_live_qr_cancel_pass' in report['window_checks']
        if args.bili_policy_check:
            assert 'check_bili_live_policy_edit_pass' in report['window_checks']
        if args.fact_check:
            assert 'check_fact_timeline_confirmation_pass' in report['window_checks']
        if args.snapshot_check:
            assert 'check_snapshot_collection_retains_used_history_pass' in report['window_checks']
        report['original_ciphertext_sha256']=digest
        after={p.relative_to(exe.parent).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
               for p in exe.parent.rglob('*') if p.is_file()}
        assert before==after
        assert not (root/'database/postmaster.pid').exists()
        lifecycle=[json.loads(line) for line in (profile/'logs/events.jsonl').read_text(encoding='utf8').splitlines()]
        assert any(e['name']=='recovery_check_started' for e in lifecycle)
        assert any(e['name']=='recovery_check_completed' for e in lifecycle)
        # The full smoke deliberately crashes its backend. That run must remain
        # abnormal even if it subsequently restarts and cleans up successfully.
        assert read(profile/'data/application-session.json')['Stage']!='clean'
        scope.close()
        scope=ProcessScope()
        process=subprocess.Popen([str(exe),'--recovery-check-smoke','--data-dir',str(profile)],cwd=evidence,env=env,
            stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW|0x4)
        scope.attach(process)
        scope.resume()
        assert process.wait(timeout=120)==0
        assert read(profile/'data/application-session.json')['Stage']=='clean'
        assert json.loads(vault.get_password(VAULT,state['installation']))==credential
        assert not (root/'database/postmaster.pid').exists()
        report['abnormal_exit_detected_recovery_checked_and_clean_exit_recorded']=True
        report['passed']=True
    finally:
        try:
            scope.close()
        finally:
            for handle in handles.values():
                api.CloseHandle(handle)
            if (root/'installation.json').exists():
                identity=read(root/'installation.json')['installation']
                if vault.get_password(VAULT,identity) is not None:
                    vault.delete_password(VAULT,identity)
                report['test_credentials_removed']=vault.get_password(VAULT,identity) is None
            (evidence/'managed-launch-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))


if __name__=='__main__':
    main()
