"""Retirement never evicts useful history or resurrects older assertions."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from companion_core import fact_retirement as retirement
from companion_core import postgres_transfer
from companion_core.errors import AssistantError
from companion_core.governance import GovernedContext
from companion_core.temporal_facts import PersonalFacts
from tests_postgres.test_desktop_facts import correction, current_fact, history
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_fact_trace_governance import facts, seed
from tests_postgres.test_personal_fact_management import seed_chain

__all__ = ['desktop_store', 'service']


def stop(service, identifier, request=None):
    preview = call(service, 'records.delete_preview', {'kind': 'memories', 'identifier': identifier})
    assert preview['ok'], preview
    args = {'token': preview['result']['token']}
    return call(service, 'records.delete', args, request_id=request), args


def counts(store):
    with store.db() as db:
        return {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('content_records', 'content_history', 'personal_fact_index', 'state',
                              'governance_policies', 'source_dependencies', 'governance_audit')}


def later():
    return datetime.now(timezone.utc) + timedelta(days=91)


def test_stop_all_old_and_future_values_no_resurrection_and_archive_survives_restore(service):
    seed(service.store)
    first = current_fact(service)['id']
    corrected = call(service, 'facts.correct', correction(service, first))['result']['id']
    other = facts(service.store, 'other-user', '我住深圳', user='b')[2]
    other.save([{'attribute': 'residence', 'value': '深圳', 'quote': '我住深圳', 'valid_from': None, 'supersedes': []}])
    request = uuid.uuid4().hex
    result, args = stop(service, corrected, request)
    assert result['ok'] and result['result']['count'] == 2
    snapshot = counts(service.store)
    for _ in range(100):
        assert call(service, 'records.delete', args, request_id=request) == result
    assert counts(service.store) == snapshot
    assert not facts(service.store, 'read-after-stop', '住所')[2].context()['items']
    assert facts(service.store, 'read-other', '住所', user='b')[2].context()['items']
    assert service.store.exists('memories', first)
    assert all(not service.repository.policy('memories/' + key).valid for key in (first, corrected))
    archive = history(service, corrected)['result']
    assert archive['counts']['disabled'] == 2
    assert {row['value'] for row in archive['items']} == {'上海', '杭州'}
    assert not call(service, 'facts.correct', correction(service, corrected))['ok']
    blob = postgres_transfer.create(service.store, service.store._backup_key)
    postgres_transfer.restore(service.store, blob, service.store._backup_key,
                              expected=postgres_transfer.fingerprint(service.store))
    assert not facts(service.store, 'read-restored', '住所')[2].context()['items']
    assert history(service, corrected)['result']['counts']['disabled'] == 2
    repeated, _ = stop(service, corrected)
    assert repeated['result']['already_retired']


def test_unretired_old_history_is_never_cleaned_and_archive_is_pageable(service):
    identifier = seed_chain(service.store, 23)
    before = counts(service.store)
    assert retirement.cleanup(service.store, clock=later)['removed'] == 0
    assert counts(service.store)['content_records'] == before['content_records']
    assert history(service, identifier)['result']['total'] == 23
    result, _ = stop(service, identifier)
    assert result['result']['count'] == 23
    page = history(service, identifier)['result']
    rest = history(service, identifier, after=page['next'])['result']
    assert len(page['items']) == 20 and len(rest['items']) == 3
    assert page['revision'] == rest['revision']
    assert all(row['status'] == 'disabled' for row in page['items'] + rest['items'])


def test_stop_covers_future_values_but_not_another_attribute(service):
    seed(service.store)
    identifier = current_fact(service)['id']
    future = facts(service.store, 'future', '我将于2035-01-01住南京')[2]
    assert future.save([{'attribute': 'residence', 'value': '南京', 'quote': '我将于2035-01-01住南京',
                         'valid_from': '2035-01-01', 'supersedes': []}])['written'] == 1
    drink = facts(service.store, 'drink', '我喜欢喝茶')[2]
    assert drink.save([{'attribute': 'drink_preference', 'value': '茶', 'quote': '我喜欢喝茶',
                        'valid_from': None, 'supersedes': []}])['written'] == 1
    result, _ = stop(service, identifier)
    assert result['result']['count'] == 2
    page = history(service, identifier)['result']
    assert {row['value'] for row in page['items']} == {'上海', '南京'}
    remaining = facts(service.store, 'after-future-stop', '偏好')[2].context()['items']
    assert len(remaining) == 1 and remaining[0]['attribute'] == 'drink_preference'


def test_cleanup_only_explicit_expired_unreferenced_and_old_replay_stays_blocked(service):
    # Read through SQL metadata to avoid manufacturing an immutable projection dependency.
    event, _ = seed(service.store)
    with service.store.db() as db:
        identifier = db.execute('SELECT id FROM personal_fact_index').fetchone()[0]
    original = service.store.read('memories', identifier)
    assert stop(service, identifier)[0]['ok']
    assert retirement.cleanup(service.store)['removed'] == 0
    result = retirement.cleanup(service.store, clock=later)
    assert result['removed'] == 1
    assert not service.store.exists('memories', identifier)
    assert retirement.retired(service.store, identifier) is None
    assert service.store.state(retirement.GUARD + retirement.identity(service.store, original['data']['fact']))
    assert not service.repository.policy('memories/' + identifier).valid
    with service.store.db() as db:
        assert not db.execute('SELECT 1 FROM personal_fact_index WHERE id=?', (identifier,)).fetchone()
        assert not db.execute('SELECT 1 FROM content_history WHERE id=?', (identifier,)).fetchone()
    replay = PersonalFacts(GovernedContext(service.store, event), event, lambda: datetime.now(timezone.utc))
    assert replay.save([{'attribute': 'residence', 'value': '上海', 'quote': event.text,
                        'valid_from': None, 'supersedes': []}]) == {'written': 0, 'rejected': 1}
    fresh = facts(service.store, 'later-new-message', '我住北京')[2]
    assert fresh.save([{'attribute': 'residence', 'value': '北京', 'quote': '我住北京',
                       'valid_from': None, 'supersedes': []}])['written'] == 1
    assert current_fact(service)['value'] == '北京'


def test_referenced_discarded_body_retained_even_after_retention(service):
    seed(service.store)
    identifier = current_fact(service)['id']  # A real fact-view now references it.
    assert stop(service, identifier)[0]['ok']
    result = retirement.cleanup(service.store, clock=later)
    assert result['removed'] == 0 and result['referenced_or_active'] == 1
    assert service.store.exists('memories', identifier)


def test_changed_scope_invalidates_delete_preview(service):
    seed(service.store)
    identifier = current_fact(service)['id']
    preview = call(service, 'records.delete_preview', {'kind': 'memories', 'identifier': identifier})['result']
    assert call(service, 'facts.correct', correction(service, identifier))['ok']
    result = call(service, 'records.delete', {'token': preview['token']})
    assert result['error'] == 'edit_conflict'
    assert not retirement.retired(service.store, identifier)
    assert current_fact(service)['value'] == '杭州'


def test_retirement_failure_rolls_back_policies_markers_and_receipt(service, monkeypatch):
    seed(service.store)
    identifier = current_fact(service)['id']
    expected = retirement.preview(service.store, identifier)['fingerprint']
    before = counts(service.store)
    original = service.store.set_state
    def fail(key, *args, **kwargs):
        if key.startswith(retirement.GUARD):
            raise AssistantError('injected_failure', 'synthetic interruption')
        return original(key, *args, **kwargs)
    monkeypatch.setattr(service.store, 'set_state', fail)
    with pytest.raises(AssistantError, match='synthetic interruption'):
        retirement.retire(service.store, identifier, expected, 'test')
    assert counts(service.store) == before
    assert service.repository.policy('memories/' + identifier).valid
    assert not retirement.retired(service.store, identifier)


def test_cleanup_interruption_rolls_back_body_index_and_marker(service):
    seed(service.store)
    with service.store.db() as db:
        identifier = db.execute('SELECT id FROM personal_fact_index').fetchone()[0]
    assert stop(service, identifier)[0]['ok']
    before = counts(service.store)
    n = 0
    def interrupted():
        nonlocal n
        n += 1
        if n == 3:
            raise AssistantError('stale_runtime', 'synthetic shutdown after deletes')
    with pytest.raises(AssistantError):
        retirement.cleanup(service.store, clock=later, guard=interrupted)
    assert counts(service.store) == before
    assert service.store.exists('memories', identifier)
    assert retirement.retired(service.store, identifier)
    assert retirement.cleanup(service.store, clock=later)['removed'] == 1


def test_retention_versioned_deduplicated_validated_and_locked(service):
    settings = call(service, 'facts.retention')['result']
    assert settings['days'] == 90
    args = {'days': 120, 'expected': settings['revision']}
    request = uuid.uuid4().hex
    saved = call(service, 'facts.retention_save', args, request_id=request)
    assert saved['ok'] and saved['result']['days'] == 120
    assert call(service, 'facts.retention_save', args, request_id=request) == saved
    assert call(service, 'facts.retention_save', args)['error'] == 'edit_conflict'
    for days in (0, 3651, True, 2.5, '90'):
        assert not call(service, 'facts.retention_save', {'days': days, 'expected': saved['result']['revision']})['ok']
    service.enable_lock('Synthetic-Fact-Lock-2026!')
    service.lock()
    assert call(service, 'facts.retention')['error'] == 'desktop_locked'
    assert call(service, 'facts.retention_save', args)['error'] == 'desktop_locked'


def test_runtime_maintenance_runs_fact_cleanup(service, monkeypatch):
    from companion_core.media_originals import MediaOriginals
    from qiban.desktop_runtime import DesktopRuntime
    calls = []
    class Runtime:
        store = service.store
        from qiban.desktop_tasks import TaskBarrier
        tasks = TaskBarrier()
        def require_current(self):
            calls.append('guard')
    monkeypatch.setattr(MediaOriginals, 'cleanup', lambda self, guard: guard())
    runtime = Runtime()
    DesktopRuntime.maintain(runtime)
    assert service.store.state('fact-cleanup-last')['examined'] == 0
    assert len(calls) >= 3


def test_small_batches_resume_without_skipping_and_do_not_copy_history(service):
    identifier = seed_chain(service.store, 7)
    with service.store.db() as db:
        before_bytes = db.execute("SELECT COALESCE(SUM(octet_length(body)),0) FROM content_records WHERE kind='memories'").fetchone()[0]
        before_history = db.execute("SELECT COUNT(*) FROM content_history WHERE kind='memories'").fetchone()[0]
    assert stop(service, identifier)[0]['ok']
    with service.store.db() as db:
        assert db.execute("SELECT COALESCE(SUM(octet_length(body)),0) FROM content_records WHERE kind='memories'").fetchone()[0] == before_bytes
        assert db.execute("SELECT COUNT(*) FROM content_history WHERE kind='memories'").fetchone()[0] == before_history
    removed = 0
    for _ in range(30):
        result = retirement.cleanup(service.store, clock=later, limit=2)
        assert result['examined'] <= 2
        removed += result['removed']
    assert removed == 7
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM personal_fact_index').fetchone()[0] == 0
    # Watermark is shared by the complete attribute, rather than copying its history.
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM state WHERE key LIKE ?', (retirement.GUARD + '%',)).fetchone()[0] == 1


@pytest.mark.parametrize('stage', ['before_commit', 'after_commit'])
def test_process_exit_during_cleanup_has_atomic_recoverable_outcome(service, stage):
    import base64
    import json
    import subprocess
    import sys

    seed(service.store)
    with service.store.db() as db:
        identifier = db.execute('SELECT id FROM personal_fact_index').fetchone()[0]
    assert stop(service, identifier)[0]['ok']
    script = '''
import base64,json,os,sys
from datetime import datetime,timedelta,timezone
from companion_core.postgres import PostgresStore
from companion_core.fact_retirement import cleanup
v=json.load(sys.stdin)
store=PostgresStore(v['root'],v['options'],base64.b64decode(v['key']))
n=0
def guard():
    global n
    n+=1
    if n==3 and v['stage']=='before_commit':os._exit(96)
result=cleanup(store,guard=guard,clock=lambda:datetime.now(timezone.utc)+timedelta(days=91))
assert result['removed']==1
os._exit(96)
'''
    payload = {'root': str(service.store.root), 'options': service.store.database._options,
               'key': base64.b64encode(service.store._test_key).decode(), 'stage': stage}
    child = subprocess.run([sys.executable, '-c', script], input=json.dumps(payload), text=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.returncode == 96
    exists = stage == 'before_commit'
    assert service.store.exists('memories', identifier) == exists
    assert bool(retirement.retired(service.store, identifier)) == exists
    assert retirement.cleanup(service.store, clock=later)['removed'] == int(exists)
    assert not service.repository.policy('memories/' + identifier).valid
