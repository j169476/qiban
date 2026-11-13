"""D47: administrator correction preserves document locks and provenance."""
import uuid
from dataclasses import replace

import pytest

from companion_core import postgres_transfer
from companion_core.access import AccessService, RecordPolicy, RequestAuthority
from companion_core.errors import AssistantError
from qiban.desktop_profile import desktop_profile
from tests_postgres.test_desktop_management import call, desktop_store, service

__all__ = ['desktop_store', 'service']


@pytest.fixture
def pg_profile():
    return desktop_profile()


def document(kind, identifier='locked-document-147'):
    value = {'schema_version': 1, 'id': identifier, 'content': '合成的已锁定资料中文123🙂', 'tags': ['原标签'], 'locked': True,
             'source': {'type': 'user_instruction', 'recorded_at': '2026-11-07T00:00:00Z', 'reference': 'synthetic'}}
    if kind == 'knowledge':
        value.update(coverage={'kind': 'web', 'bvid': '', 'start_seconds': 0, 'end_seconds': 0,
                               'duration_seconds': 0, 'complete': False}, disagrees_with=['other-note'])
    if kind == 'diaries':
        value['memory_refs'] = [{'id': 'memory-original', 'version': 'a' * 64}]
    return value


def prepare(service, kind):
    value = document(kind)
    service.store.write(kind, value, None, 'test', value['id'])
    old = service.store.read(kind, value['id'])
    resource = kind + '/' + value['id']
    policy = service.repository.policy(resource)
    service.repository.set_policy(replace(policy, classification='public'), policy.version, 'test')
    return old, {'kind': kind, 'identifier': value['id'], 'expected': old['version'],
                 'value': {**value, 'content': '管理员更正中文456🙂', 'tags': ['新标签']}}


@pytest.mark.parametrize('kind', ['knowledge', 'diaries'])
def test_admin_edits_locked_document_preserves_metadata_history_policy_and_role_lock(service, kind):
    old, args = prepare(service, kind)
    resource = kind + '/' + args['identifier']
    policy = service.repository.policy(resource)
    service.repository.set_policy(RecordPolicy('derived/document', 'derived-v1', 'public',
        sources=(resource,), source_versions=((resource, policy.version),)), None, 'test')
    authority = RequestAuthority(epoch=service.repository.epoch)
    request = authority.issue(actor=None, conversation='public', audience='public', purpose='exploration')
    access = AccessService(service.repository, authority)
    lease = access.lease(request, ['derived/document'])
    request_id = uuid.uuid4().hex
    saved = call(service, 'records.correct', args, request_id=request_id)
    assert saved['ok'], saved
    assert call(service, 'records.correct', args, request_id=request_id) == saved
    assert call(service, 'records.correct', args)['error'] == 'edit_conflict'
    current = service.store.read(kind, args['identifier'])
    assert current['data'] == args['value'] and current['data']['locked']
    assert current['data']['source'] == old['data']['source']
    current_policy = service.repository.policy(resource)
    assert replace(current_policy, version=policy.version) == policy
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM content_history WHERE kind=? AND id=? AND version=?',
                          (kind, args['identifier'], old['version'])).fetchone()[0] == 1
    with pytest.raises(AssistantError):
        access.revalidate(lease)
    for locked in (True, False):
        with pytest.raises(AssistantError, match='锁定'):
            service.store.write(kind, {**current['data'], 'locked': locked, 'content': '角色自动修改'},
                                current['version'], 'role', args['identifier'])
    assert service.store.read(kind, args['identifier']) == current
    denied = call(service, 'records.correct', {**args, 'expected': current['version'],
        'value': {**current['data'], 'locked': False}})
    assert denied['error'] == 'record_lock_preserved'


@pytest.mark.parametrize('kind', ['knowledge', 'diaries'])
def test_non_admin_and_app_lock_cannot_use_manual_override(service, kind):
    old, args = prepare(service, kind)
    request = service.authority.issue(actor=service.actor, conversation='reply', audience='private',
        recipients=(service.actor,), session_id=service.session_id)
    with pytest.raises(AssistantError):
        service.records.correct(request, kind, args['identifier'], args['value'], args['expected'])
    service.enable_lock('Synthetic-Document-Lock-2026!')
    service.lock()
    assert call(service, 'records.correct', args)['error'] == 'desktop_locked'
    assert service.store.read(kind, args['identifier']) == old


@pytest.mark.parametrize('kind', ['knowledge', 'diaries'])
def test_failed_receipt_rolls_back_body_history_policy_and_success_receipt(service, monkeypatch, kind):
    old, args = prepare(service, kind)
    policy = service.repository.policy(kind + '/' + args['identifier'])
    request_id = uuid.uuid4().hex
    real = service.store.set_state
    def fail(key, *values, **kwargs):
        if key == 'desktop-request:' + request_id:
            raise AssistantError('injected_failure', 'synthetic interruption')
        return real(key, *values, **kwargs)
    monkeypatch.setattr(service.store, 'set_state', fail)
    assert not call(service, 'records.correct', args, request_id=request_id)['ok']
    assert service.store.read(kind, args['identifier']) == old
    assert service.repository.policy(kind + '/' + args['identifier']) == policy
    assert not service.store.state('desktop-request:' + request_id)
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM content_history WHERE kind=? AND id=?',
                          (kind, args['identifier'])).fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['knowledge', 'diaries'])
def test_backup_and_delete_preview_preserve_old_record_rules(service, kind):
    _, args = prepare(service, kind)
    saved = call(service, 'records.correct', args)
    assert saved['ok']
    blob = postgres_transfer.create(service.store, service.store._backup_key)
    preview = call(service, 'records.delete_preview', {'kind': kind, 'identifier': args['identifier']})['result']
    deleted = call(service, 'records.delete', {'token': preview['token']})
    assert deleted['ok'] and deleted['result']['history_retained']
    assert not service.store.exists(kind, args['identifier'])
    postgres_transfer.restore(service.store, blob, service.store._backup_key,
                              expected=postgres_transfer.fingerprint(service.store))
    assert service.store.read(kind, args['identifier'])['data'] == args['value']


def test_concurrent_manual_edits_only_one_wins(service):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, args = prepare(service, 'knowledge')
    gate = Barrier(2)
    def write(number):
        gate.wait()
        return call(service, 'records.correct', {**args, 'value': {**args['value'], 'content': str(number)}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (1, 2)))
    assert sum(row['ok'] for row in results) == 1
    assert next(row for row in results if not row['ok'])['error'] == 'edit_conflict'
    assert service.store.read('knowledge', args['identifier'])['data']['locked']
