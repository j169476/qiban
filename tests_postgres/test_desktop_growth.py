import uuid
from types import SimpleNamespace

import pytest

from companion_core import postgres_transfer, public_growth
from companion_core.errors import AssistantError
from companion_core.public_workspace import PublicWorkspace
from qiban.desktop_management import DesktopManagement
from qiban.desktop_runtime import SESSION
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_public_growth import seed

__all__ = ['desktop_store', 'service']


def broken(service):
    seed(service.store)
    original = service.store.state('public-growth')
    record = service.store.read('relationships', 'public-person')
    service.store.write('relationships', {**record['data'], 'affinity': 41}, record['version'], 'admin', 'public-person')
    return original


def archives(service):
    return call(service, 'growth.archives', {'after': ''})['result']['items']


def test_preview_is_readonly_healthy_state_cannot_be_reset(service):
    original = service.store.state('public-growth')
    result = call(service, 'growth.preview')['result']
    assert result['reason'] == 'readable' and result['token'] is None
    assert service.store.state('public-growth') == original
    assert not archives(service)
    assert call(service, 'growth.rebuild', {'token': 'invented'})['error'] == 'edit_conflict'


def test_recovery_atomic_deduplicated_archived_and_remains_stopped(service):
    original = broken(service)
    preferences = service.store.state('desktop-autorun')
    service.store.set_state(SESSION, {'held': []})
    service.runtime = SimpleNamespace(require_current=lambda: None, scheduler=None)
    preview = call(service, 'growth.preview')['result']
    assert preview['reason'].startswith('public_growth_') and preview['data'] == service.store.profile.defaults['growth']
    assert service.store.state('public-growth') == original
    request = uuid.uuid4().hex
    result = call(service, 'growth.rebuild', {'token': preview['token']}, request_id=request)
    assert result['ok'] and result['result']['requires_explicit_start']
    for _ in range(20):
        assert call(service, 'growth.rebuild', {'token': preview['token']}, request_id=request) == result
    assert len(archives(service)) == 1
    assert service.store.state(SESSION)['held'] == ['bilibili']
    assert service.store.state('desktop-autorun') == preferences
    assert service.store.state('core-run:bilibili', {}).get('status') != 'active'
    archive = call(service, 'growth.archive', {'identifier': result['result']['archive']})['result']
    assert archive['original'] == original and archive['read_only']
    assert PublicWorkspace(service.store).read('growth')['data'] == service.store.profile.defaults['growth']
    with pytest.raises(AssistantError):
        public_growth.rebuild(service.store, preview['expected'], 'test')


@pytest.mark.parametrize('state', ['active', 'busy', 'stopping'])
def test_runtime_must_be_idle_at_preview_and_again_at_commit(service, state):
    broken(service)
    preview = call(service, 'growth.preview')['result']
    if state == 'active':
        service.store.set_state('core-run:bilibili', {'status': 'active'})
    else:
        service.runtime = SimpleNamespace(require_current=lambda: None, scheduler=SimpleNamespace(
            status=lambda platform: {'busy': state == 'busy', 'stopping': state == 'stopping'}))
    assert call(service, 'growth.rebuild', {'token': preview['token']})['error'] == 'worker_busy'
    assert call(service, 'growth.preview')['error'] == 'worker_busy'
    assert not archives(service)


def test_expired_cross_session_and_stale_previews_are_rejected(service):
    broken(service)
    preview = call(service, 'growth.preview')['result']
    other = DesktopManagement(service.store, 'installation-test', 'S-1-5-21-test-owner')
    assert call(other, 'growth.rebuild', {'token': preview['token']})['error'] == 'edit_conflict'
    service.growth.proposal['expires'] = 0
    assert call(service, 'growth.rebuild', {'token': preview['token']})['error'] == 'edit_conflict'
    preview = call(service, 'growth.preview')['result']
    service.store.set_state('public-growth', {'data': {'synthetic': 'changed'}, 'version': 'changed'})
    assert call(service, 'growth.rebuild', {'token': preview['token']})['error'] == 'edit_conflict'
    assert not archives(service)


def test_lock_blocks_archive_and_preview_and_old_token_after_unlock(service):
    broken(service)
    service.enable_lock('Synthetic-Growth-Lock-2026!')
    preview = call(service, 'growth.preview')['result']
    service.lock()
    for op, args in [('growth.preview', {}), ('growth.rebuild', {'token': preview['token']}),
                     ('growth.archives', {'after': ''}), ('growth.archive', {'identifier': 'public-growth-recovery:' + 'a' * 32})]:
        assert call(service, op, args)['error'] == 'desktop_locked'
    service.unlock('Synthetic-Growth-Lock-2026!')
    assert call(service, 'growth.rebuild', {'token': preview['token']})['error'] == 'edit_conflict'


def test_archive_pagination_and_backup_do_not_republish_old_content(service):
    original = broken(service)
    preview = call(service, 'growth.preview')['result']
    assert call(service, 'growth.rebuild', {'token': preview['token']})['ok']
    for index in range(22):
        service.store.set_state('public-growth-recovery:' + f'{index:032x}', {'original': original, 'policy_version': 'old'})
    first = call(service, 'growth.archives', {'after': ''})['result']
    second = call(service, 'growth.archives', {'after': first['next']})['result']
    assert len(first['items']) == 20 and len(second['items']) == 3 and second['next'] is None
    blob = postgres_transfer.create(service.store, service.store._backup_key)
    postgres_transfer.restore(service.store, blob, service.store._backup_key,
                              expected=postgres_transfer.fingerprint(service.store))
    assert call(service, 'growth.archive', {'identifier': first['items'][0]})['result']['original'] == original
    assert PublicWorkspace(service.store).read('growth')['data'] == service.store.profile.defaults['growth']
    for value in ['desktop-owner', '../state', None, {}, 'public-growth-recovery:a']:
        assert not call(service, 'growth.archive', {'identifier': value})['ok']


def test_failure_before_receipt_rolls_back_archive_policy_state_and_hold(service, monkeypatch):
    original = broken(service)
    service.store.set_state(SESSION, {'held': []})
    service.runtime = SimpleNamespace(require_current=lambda: None, scheduler=None)
    preview = call(service, 'growth.preview')['result']
    policy = service.repository.policy(public_growth.RESOURCE)
    request = uuid.uuid4().hex
    real = service.store.set_state
    def fail(key, *args, **kwargs):
        if key == 'desktop-request:' + request:
            raise AssistantError('injected_failure', 'synthetic interruption')
        return real(key, *args, **kwargs)
    monkeypatch.setattr(service.store, 'set_state', fail)
    assert not call(service, 'growth.rebuild', {'token': preview['token']}, request_id=request)['ok']
    assert service.store.state('public-growth') == original
    assert service.repository.policy(public_growth.RESOURCE) == policy
    assert service.store.state(SESSION)['held'] == []
    assert not service.store.state('desktop-request:' + request) and not archives(service)


@pytest.mark.parametrize('stage', ['before_commit', 'after_commit'])
def test_actual_process_exit_and_lost_reply_do_not_duplicate_archive(service, stage):
    import base64
    import json
    import subprocess
    import sys

    original = broken(service)
    request = uuid.uuid4().hex
    script = '''
import base64,json,os,sys
from companion_core.postgres import PostgresStore
from companion_core.governance import GovernanceRepository
from qiban.desktop_management import DesktopManagement
v=json.load(sys.stdin)
store=PostgresStore(v['root'],v['options'],base64.b64decode(v['key']))
manager=DesktopManagement(store,'installation-test','S-1-5-21-test-owner')
p=manager.dispatch({'action':'management','operation':'growth.preview','arguments':{},'request_id':'a'*32})['result']
if v['stage']=='before_commit':
    real=GovernanceRepository.audit
    def audit(self,actor,operation,*args,**kwargs):
        if operation=='public_growth_rebuild':os._exit(97)
        return real(self,actor,operation,*args,**kwargs)
    GovernanceRepository.audit=audit
r=manager.dispatch({'action':'management','operation':'growth.rebuild','arguments':{'token':p['token']},'request_id':v['request']})
assert r['ok']
os._exit(97)
'''
    payload = {'root': str(service.store.root), 'options': service.store.database._options,
               'key': base64.b64encode(service.store._test_key).decode(), 'stage': stage, 'request': request}
    child = subprocess.run([sys.executable, '-c', script], input=json.dumps(payload), text=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.returncode == 97
    assert len(archives(service)) == int(stage == 'after_commit')
    receipt = service.store.state('desktop-request:' + request)
    assert bool(receipt) == (stage == 'after_commit')
    if stage == 'before_commit':
        assert service.store.state('public-growth') == original
        preview = call(service, 'growth.preview')['result']
        assert call(service, 'growth.rebuild', {'token': preview['token']})['ok']
    else:
        assert call(service, 'growth.preview')['result']['token'] is None
        assert PublicWorkspace(service.store).read('growth')['data'] == service.store.profile.defaults['growth']
    assert len(archives(service)) == 1
