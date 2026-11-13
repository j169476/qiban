"""Desktop fact timelines retain the existing provenance and confirmation rules."""
import uuid
from dataclasses import replace

import pytest

from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_fact_trace_governance import facts, seed
from tests_postgres.test_personal_fact_management import seed_chain

__all__ = ['desktop_store', 'service']


def history(service, key, **overrides):
    return call(service, 'facts.history', {'identifier': key, 'after': '', 'at': None, **overrides})


def test_confirmation_is_append_only_deduplicated_and_version_checked(service):
    seed(service.store)
    other = facts(service.store, 'two', '我住杭州')[2]
    other.save([{'attribute': 'residence', 'value': '杭州', 'quote': '我住杭州',
                 'valid_from': None, 'supersedes': []}])
    rows = other.context()['items']
    chosen = next(row for row in rows if row['value'] == '杭州')
    before = {row['id']: service.store.read('memories', row['id']) for row in rows}
    page = history(service, chosen['id'])['result']
    args = {'identifier': chosen['id'], 'expected': page['revision'],
            'reason': '管理员核实中文123🙂', 'supersedes': list(before)}
    request = uuid.uuid4().hex
    saved = call(service, 'facts.confirm', args, request_id=request)
    assert saved['ok'] and saved['result']['history_preserved']
    assert call(service, 'facts.confirm', args, request_id=request) == saved
    assert call(service, 'facts.confirm', args)['error'] == 'edit_conflict'
    assert all(service.store.read('memories', key) == value for key, value in before.items())
    final = history(service, saved['result']['id'])['result']
    assert final['counts']['active'] == 1 and final['counts']['historical'] == 2
    assert len(final['items']) == 3
    assert service.repository.policy('memories/' + saved['result']['id']).classification == 'personal'


def test_history_is_bounded_and_accepts_historical_time(service):
    identifier = seed_chain(service.store, 23)
    first = history(service, identifier)['result']
    second = history(service, identifier, after=first['next'])['result']
    assert len(first['items']) == 20 and len(second['items']) == 3
    assert not ({row['id'] for row in first['items']} & {row['id'] for row in second['items']})
    past = history(service, identifier, at='2026-01-01T00:30:00+00:00')['result']
    assert past['counts']['active'] == 1 and past['counts']['future'] == 22
    assert history(service, identifier, at='2026-01-01')['error'] == 'invalid_query'


def test_lock_and_revoked_source_block_fact_access(service):
    event, governed = seed(service.store)
    identifier = facts(service.store, 'read', '住所')[2].context()['items'][0]['id']
    page = history(service, identifier)['result']
    args = {'identifier': identifier, 'expected': page['revision'], 'reason': '核实', 'supersedes': [identifier]}
    policy = governed.repository.policy('event/' + event.id)
    governed.repository.set_policy(replace(policy, version=uuid.uuid4().hex, valid=False), policy.version, 'test')
    assert not history(service, identifier)['ok']
    assert not call(service, 'facts.confirm', args)['ok']
    service.enable_lock('Synthetic-Fact-Lock-2026!')
    service.lock()
    assert history(service, identifier)['error'] == 'desktop_locked'
    assert call(service, 'facts.confirm', args)['error'] == 'desktop_locked'


@pytest.mark.parametrize('changes', [{'identifier': {}}, {'after': []}, {'at': 7}, {'extra': True}])
def test_invalid_request_is_rejected(service, changes):
    assert not history(service, 'fact-test', **changes)['ok']


def correction(service, identifier, value='杭州'):
    page = history(service, identifier)['result']
    return {'identifier': identifier, 'expected': page['revision'], 'value': value,
            'reason': '管理员核对后更正123🙂', 'supersedes': [identifier]}


def current_fact(service):
    rows = facts(service.store, uuid.uuid4().hex, '住所')[2].context()['items']
    return next(row for row in rows if row['status'] == 'active')


def test_correction_preserves_original_and_model_uses_new_value_across_restore(service):
    from companion_core import postgres_transfer

    seed(service.store)
    identifier = current_fact(service)['id']
    original = service.store.read('memories', identifier)
    args = correction(service, identifier)
    request = uuid.uuid4().hex
    result = call(service, 'facts.correct', args, request_id=request)
    assert result['ok']
    assert call(service, 'facts.correct', args, request_id=request) == result
    first = result['result']['id']
    projected = current_fact(service)
    assert projected['id'] == first and projected['value'] == '杭州' and projected['quote'] == '我住上海'
    assert projected['correction']['original_value'] == '上海'
    assert projected['correction']['original'] == identifier
    assert service.store.read('memories', identifier) == original
    assert service.repository.policy('memories/' + first).classification == 'personal'
    blob = postgres_transfer.create(service.store, service.store._backup_key)
    second = call(service, 'facts.correct', correction(service, first, '北京'))
    assert second['ok'] and current_fact(service)['value'] == '北京'
    assert history(service, first)['result']['counts'] == {'active': 1, 'disputed': 0, 'historical': 2, 'future': 0}
    postgres_transfer.restore(service.store, blob, service.store._backup_key,
                              expected=postgres_transfer.fingerprint(service.store))
    assert current_fact(service)['id'] == first
    assert not service.store.exists('memories', second['result']['id'])
    assert service.store.read('memories', identifier) == original


def test_failed_write_rolls_back_record_index_scope_and_receipt(service, monkeypatch):
    from companion_core.errors import AssistantError

    seed(service.store)
    identifier = current_fact(service)['id']
    args = correction(service, identifier)
    def snapshot():
        with service.store.db() as db:
            return {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                    for table in ('content_records', 'personal_fact_index', 'governance_policies')}
    before = snapshot()
    request = uuid.uuid4().hex
    def interrupted(*args, **kwargs):
        raise AssistantError('synthetic_interruption', '模拟保存中断')
    with monkeypatch.context() as patch:
        patch.setattr('companion_core.governance.GovernedContext.classify_written', interrupted)
        assert call(service, 'facts.correct', args, request_id=request)['error'] == 'synthetic_interruption'
    assert snapshot() == before
    assert service.store.state('desktop-request:' + request) is None
    assert current_fact(service)['id'] == identifier
    assert call(service, 'facts.correct', args, request_id=request)['ok']
    assert current_fact(service)['value'] == '杭州'


def test_concurrent_corrections_have_one_winner(service):
    from concurrent.futures import ThreadPoolExecutor

    seed(service.store)
    args = correction(service, current_fact(service)['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda value: call(service, 'facts.correct', {**args, 'value': value}), ['杭州', '北京']))
    assert sum(row['ok'] for row in outcomes) == 1
    assert next(row for row in outcomes if not row['ok'])['error'] == 'edit_conflict'
    page = history(service, args['identifier'])['result']
    assert page['total'] == 2 and page['counts']['active'] == 1


def test_generic_editor_cannot_overwrite_fact_or_remove_its_structured_fields(service):
    seed(service.store)
    identifier = current_fact(service)['id']
    before = service.store.read('memories', identifier)
    for value in ({**before['data'], 'content': '直接覆盖'},
                  {k: v for k, v in before['data'].items() if k != 'fact'}):
        result = call(service, 'records.correct', {'kind': 'memories', 'identifier': identifier,
                      'value': value, 'expected': before['version']})
        assert result['error'] == 'fact_append_required'
    assert service.store.read('memories', identifier) == before


@pytest.mark.parametrize('value', ['', '   ', '上海', '字' * 161, None, {}])
def test_invalid_correction_never_writes(service, value):
    seed(service.store)
    identifier = current_fact(service)['id']
    assert call(service, 'facts.correct', correction(service, identifier, value))['error'] == 'invalid_query'
    assert history(service, identifier)['result']['total'] == 1


def test_correction_is_unavailable_after_source_revocation_or_lock(service):
    event, governed = seed(service.store)
    identifier = current_fact(service)['id']
    args = correction(service, identifier)
    policy = governed.repository.policy('event/' + event.id)
    governed.repository.set_policy(replace(policy, version=uuid.uuid4().hex, valid=False), policy.version, 'test')
    assert not call(service, 'facts.correct', args)['ok']
    service.enable_lock('Synthetic-Correction-Lock-2026!')
    service.lock()
    assert call(service, 'facts.correct', args)['error'] == 'desktop_locked'


@pytest.mark.parametrize('stage', ['before_commit', 'after_commit'])
def test_process_exit_mid_save_or_after_commit_has_one_recoverable_outcome(service, stage):
    import base64
    import json
    import subprocess
    import sys

    seed(service.store)
    identifier = current_fact(service)['id']
    args = correction(service, identifier)
    request = uuid.uuid4().hex
    script = '''
import base64,json,os,sys
from companion_core.postgres import PostgresStore
from companion_core.governance import GovernedContext
from qiban.desktop_management import DesktopManagement
v=json.load(sys.stdin)
store=PostgresStore(v['root'],v['options'],base64.b64decode(v['key']))
manager=DesktopManagement(store,'installation-test','S-1-5-21-test-owner')
if v['stage']=='before_commit':
    def interrupted(*args,**kwargs):os._exit(95)
    GovernedContext.classify_written=interrupted
result=manager.dispatch({'action':'management','operation':'facts.correct','arguments':v['args'],'request_id':v['request']})
assert result['ok']
os._exit(95)
'''
    payload = {'root': str(service.store.root), 'options': service.store.database._options,
               'key': base64.b64encode(service.store._test_key).decode(), 'stage': stage,
               'args': args, 'request': request}
    child = subprocess.run([sys.executable, '-c', script], input=json.dumps(payload), text=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.returncode == 95
    page = history(service, identifier)['result']
    assert page['total'] == (1 if stage == 'before_commit' else 2)
    assert bool(service.store.state('desktop-request:' + request)) == (stage == 'after_commit')
    result = call(service, 'facts.correct', args, request_id=request)
    assert result['ok'] and current_fact(service)['id'] == result['result']['id']
    assert history(service, identifier)['result']['total'] == 2
