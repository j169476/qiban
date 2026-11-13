"""Read-only provenance inspection through the desktop administrator boundary."""
import uuid
from dataclasses import replace

import pytest

from tests_postgres.test_desktop_management import call, desktop_store, memory_args, service

__all__ = ['desktop_store', 'service']


def test_explanation_tracks_source_changes_without_writing(service):
    for identifier in ('original', 'derived'):
        assert call(service, 'records.correct', memory_args(identifier))['ok']
    repo = service.repository
    original = repo.policy('memories/original')
    repo.set_policy(replace(original, version=uuid.uuid4().hex, classification='public'), original.version, 'test')
    original = repo.policy(original.resource)
    derived = repo.policy('memories/derived')
    repo.set_policy(replace(derived, version=uuid.uuid4().hex, classification='public',
                            sources=(original.resource,), source_versions=((original.resource, original.version),)),
                    derived.version, 'test')
    before = service.store.read('memories', 'derived')
    policy = repo.policy(derived.resource)
    args = {'kind': 'memories', 'identifier': 'derived'}
    result = call(service, 'records.explain', args)
    assert result['ok'] and result['result']['total_nodes'] == 2
    assert result['result']['total_issues'] == 0
    assert result['result']['impact']['affected_count'] == 1
    original_data = service.store.read('memories', 'original')
    patch = memory_args('original', '来源已修改🙂')
    patch['expected'] = original_data['version']
    assert call(service, 'records.correct', patch)['ok']
    changed = call(service, 'records.explain', args)['result']
    assert any(issue['reason'] == 'source_missing_or_changed' for issue in changed['issues'])
    assert service.store.read('memories', 'derived') == before
    assert repo.policy(derived.resource) == policy


def test_inspection_requires_unlocked_local_administrator(service):
    args = {'kind': 'persona', 'identifier': None}
    assert not call(service, 'records.explain', args, administrator=True)['ok']
    service.enable_lock('Synthetic-Inspection-Lock-2026!')
    service.lock()
    assert call(service, 'records.explain', args)['error'] == 'desktop_locked'


@pytest.mark.parametrize('args,error', [
    ({'kind': 3, 'identifier': 'one'}, 'management_request'),
    ({'kind': 'memories', 'identifier': {}}, 'management_request'),
    ({'kind': 'missing', 'identifier': 'one'}, 'invalid_query'),
    ({'kind': 'memories', 'identifier': 'absent'}, 'not_found'),
    ({'kind': 'memories', 'identifier': 'one', 'public': True}, 'management_operation'),
])
def test_invalid_inspection_cannot_change_scope(service, args, error):
    assert call(service, 'records.explain', args)['error'] == error
