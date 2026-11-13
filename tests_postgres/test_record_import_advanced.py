"""D53: previewed content and provenance changes commit as one transaction."""
import json
import uuid
from dataclasses import replace

import pytest
import yaml

from companion_core.access import RecordPolicy
from companion_core.errors import AssistantError
from qiban.desktop_profile import desktop_profile
from tests_postgres.test_desktop_management import call, desktop_store, memory_args, service

__all__ = ['desktop_store', 'service']


@pytest.fixture
def pg_profile():
    return desktop_profile()


def seed(service, identifier='import-target', kind='memories'):
    data = memory_args(identifier)['value']
    if kind != 'memories':
        for field in ('kind', 'confidence'):
            data.pop(field)
    service.store.write(kind, data, None, 'test', identifier)
    return service.store.read(kind, identifier)


def export(service, identifier='import-target', kind='memories'):
    value = call(service, 'records.advanced_export', {'kind': kind, 'identifier': identifier})
    assert value['ok'], value
    return yaml.safe_load(value['result']['text'])


def preview(service, value):
    return call(service, 'records.advanced_preview', {'kind': value['kind'], 'identifier': value['data']['id'],
        'expected_version': value['record_version'], 'text': yaml.safe_dump(value, allow_unicode=True)})


def ref(service, identifier, kind='memories'):
    resource = kind + '/' + identifier
    return {'resource': resource, 'version': service.store.read(kind, identifier)['version'],
            'policy_version': service.repository.policy(resource).version}


@pytest.mark.parametrize('kind', ['memories', 'knowledge', 'diaries'])
def test_roundtrip_noop_then_atomic_edit(service, kind):
    old = seed(service, kind=kind)
    seed(service, 'source')
    value = export(service, kind=kind)
    same = preview(service, value)
    assert same['ok'] and not same['result']['changed']
    assert call(service, 'records.advanced_commit', {'token': same['result']['token']})['ok']
    value['data']['content'] = '已核对中文123🙂'
    value['data']['source']['reference'] = '手动核对来源，不从此字符串推断引用'
    value['references'] = [ref(service, 'source')]
    original_policy = service.repository.policy(kind + '/import-target')
    p = preview(service, value)
    assert p['ok'], p
    p = p['result']
    assert p['written'] is False and p['references']['added'] == ['memories/source']
    assert service.store.read(kind, 'import-target') == old
    diff = call(service, 'records.advanced_changes', {'token': p['token'], 'offset': 0})
    assert '已核对中文123🙂' in diff['result']['text']
    request_id = uuid.uuid4().hex
    result = call(service, 'records.advanced_commit', {'token': p['token']}, request_id=request_id)
    assert result['ok'], result
    assert call(service, 'records.advanced_commit', {'token': p['token']}, request_id=request_id) == result
    updated = service.repository.policy(kind + '/import-target')
    assert updated.sources == ('memories/source',)
    assert replace(updated, version=original_policy.version, sources=original_policy.sources,
                   source_versions=original_policy.source_versions) == original_policy
    archive = service.store.state('record-import-history:' + p['token'])
    assert archive['before']['sources'] == [] and archive['after']['sources'] == ['memories/source']
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM content_history WHERE kind=? AND id=? AND version=?',
            (kind, 'import-target', old['version'])).fetchone()[0] == 1


@pytest.mark.parametrize('change', ['id', 'locked', 'schema_version', 'wrong_type', 'duplicate', 'missing', 'version', 'self'])
def test_reject_malformed_or_unverifiable_import(service, change):
    old = seed(service)
    seed(service, 'source')
    value = export(service)
    if change in ('id', 'locked', 'schema_version'):
        value['data'][change] = {'id': 'different', 'locked': True, 'schema_version': 2}[change]
    elif change == 'wrong_type':
        value['kind'] = 'diaries'
    else:
        value['references'] = [ref(service, 'source')]
        if change == 'duplicate':
            value['references'] *= 2
        if change == 'missing':
            value['references'][0]['resource'] = 'memories/missing'
        if change == 'version':
            value['references'][0]['version'] = 'a' * 64
        if change == 'self':
            value['references'] = [ref(service, 'import-target')]
    assert not preview(service, value)['ok']
    assert service.store.read('memories', 'import-target') == old


@pytest.mark.parametrize('mutation', ['target', 'source', 'impact', 'lock', 'expiry', 'cancel'])
def test_preview_cannot_commit_after_change(service, mutation):
    seed(service)
    seed(service, 'source')
    value = export(service)
    value['data']['content'] = '待确认的修改'
    value['references'] = [ref(service, 'source')]
    p = preview(service, value)['result']
    if mutation in ('target', 'source'):
        key = 'import-target' if mutation == 'target' else 'source'
        item = service.store.read('memories', key)
        service.store.write('memories', {**item['data'], 'content': '其他修改'}, item['version'], 'test', key)
    elif mutation == 'impact':
        policy = service.repository.policy('memories/import-target')
        service.repository.set_policy(RecordPolicy('derived/new', 'v1', 'public',
            sources=(policy.resource,), source_versions=((policy.resource, policy.version),)), None, 'test')
    elif mutation == 'lock':
        service.enable_lock('long-enough-password')
        service.lock()
    elif mutation == 'expiry':
        service.records.advanced.previews[p['token']]['expires'] = 0
    else:
        assert call(service, 'records.advanced_cancel', {'token': p['token']})['ok']
    assert not call(service, 'records.advanced_commit', {'token': p['token']})['ok']
    assert service.store.read('memories', 'import-target')['data']['content'] != '待确认的修改'


def test_cycle_and_diary_versions_are_checked(service):
    seed(service)
    seed(service, 'source')
    policy = service.repository.policy('memories/source')
    target = service.repository.policy('memories/import-target')
    service.repository.set_policy(replace(policy, sources=(target.resource,),
        source_versions=((target.resource, target.version),)), policy.version, 'test')
    value = export(service)
    value['references'] = [ref(service, 'source')]
    assert '循环' in preview(service, value)['message']
    seed(service, kind='diaries')
    value = export(service, kind='diaries')
    value['data']['memory_refs'] = [{'id': 'source', 'version': 'a' * 64}]
    value['references'] = [ref(service, 'source')]
    assert not preview(service, value)['ok']


def test_source_only_change_and_receipt_failure_roll_back_then_retry(service, monkeypatch):
    old = seed(service)
    seed(service, 'source')
    value = export(service)
    value['references'] = [ref(service, 'source')]
    p = preview(service, value)['result']
    original = service.repository.policy('memories/import-target')
    original_state = service.store.set_state
    request_id = uuid.uuid4().hex
    def fail_receipt(key, *args, **kwargs):
        if key == 'desktop-request:' + request_id:
            raise AssistantError('database_error', 'synthetic receipt interruption')
        return original_state(key, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(service.store, 'set_state', fail_receipt)
        assert not call(service, 'records.advanced_commit', {'token': p['token']}, request_id=request_id)['ok']
    assert service.store.read('memories', 'import-target') == old
    assert service.repository.policy('memories/import-target') == original
    assert service.store.state('record-import-history:' + p['token']) is None
    result = call(service, 'records.advanced_commit', {'token': p['token']}, request_id=request_id)
    assert result['ok'] and result['result']['changed'], result
    assert service.store.state('record-import-history:' + p['token'])['before']['sources'] == []


def test_full_difference_pages_preserve_unicode(service):
    seed(service)
    value = export(service)
    value['data']['content'] = '🙂abcdef' * 2000
    p = preview(service, value)['result']
    assert p['changes'][0]['after']['truncated']
    parts, offset = [], 0
    while offset is not None:
        part = call(service, 'records.advanced_changes', {'token': p['token'], 'offset': offset})['result']
        parts.append(part['text'])
        offset = part['next']
    assert value['data']['content'] in ''.join(parts)
    assert len(parts) > 1


def test_existing_internal_source_preserved_but_new_opaque_source_rejected(service):
    seed(service)
    service.repository.set_policy(RecordPolicy('derived/opaque', 'v1', 'public'), None, 'test')
    value = export(service)
    value['references'] = [{'resource': 'derived/opaque', 'version': None, 'policy_version': 'v1'}]
    assert not preview(service, value)['ok']
    policy = service.repository.policy('memories/import-target')
    service.repository.set_policy(replace(policy, sources=('derived/opaque',),
        source_versions=(('derived/opaque', 'v1'),)), policy.version, 'test')
    value = export(service)
    value['data']['content'] = '保留已有来源'
    assert preview(service, value)['ok']


@pytest.mark.parametrize('invalid', ['revoked', 'expired', 'upstream_changed'])
def test_invalid_source_chain_is_rejected(service, invalid):
    seed(service)
    seed(service, 'source')
    policy = service.repository.policy('memories/source')
    updates = {'valid': False} if invalid == 'revoked' else {'expires_at': 1}
    if invalid == 'upstream_changed':
        seed(service, 'upstream')
        updates = {'sources': ('memories/upstream',), 'source_versions': (('memories/upstream', 'stale'),)}
    service.repository.set_policy(replace(policy, **updates), policy.version, 'test')
    value = export(service)
    value['references'] = [ref(service, 'source')]
    assert not preview(service, value)['ok']


def test_replace_remove_refs_and_metadata_preserve_root_authorization(service):
    seed(service)
    seed(service, 'one')
    seed(service, 'two')
    target = service.repository.policy('memories/import-target')
    one = ref(service, 'one')
    service.repository.set_policy(replace(target, classification='personal', owners=frozenset({'actor-a'}),
        conversation='private-a', audience='private', sources=(one['resource'],),
        source_versions=((one['resource'], one['policy_version']),)), target.version, 'test')
    original = service.repository.policy(target.resource)
    value = export(service)
    value['data'].update(kind='experience', confidence='explicit', tags=['中文🙂'])
    value['references'] = [ref(service, 'two')]
    p = preview(service, value)['result']
    assert p['references'] == {'added': ['memories/two'], 'removed': ['memories/one'], 'updated': []}
    assert call(service, 'records.advanced_commit', {'token': p['token']})['ok']
    current = service.repository.policy(target.resource)
    assert replace(current, version=original.version, sources=original.sources,
                   source_versions=original.source_versions) == original
    value = export(service)
    value['references'] = []
    p = preview(service, value)['result']
    assert call(service, 'records.advanced_commit', {'token': p['token']})['ok']
    assert service.repository.policy(target.resource).sources == ()


def test_plain_legacy_file_does_not_implicitly_erase_sources(service):
    seed(service)
    value = export(service)
    response = call(service, 'records.advanced_preview', {'kind': 'memories', 'identifier': 'import-target',
        'expected_version': value['record_version'], 'text': yaml.safe_dump(value['data'], allow_unicode=True)})
    assert not response['ok']


@pytest.mark.parametrize('kind', ['knowledge', 'diaries'])
def test_locked_documents_remain_locked_after_import(service, kind):
    item = seed(service, kind=kind)
    service.store.write(kind, {**item['data'], 'locked': True}, item['version'], 'test', 'import-target')
    value = export(service, kind=kind)
    value['data']['content'] = '管理员修正已锁定资料'
    p = preview(service, value)['result']
    assert call(service, 'records.advanced_commit', {'token': p['token']})['ok']
    assert service.store.read(kind, 'import-target')['data']['locked'] is True


def test_exported_file_rejects_changed_target_permissions_and_missing_explicit_references(service):
    seed(service)
    value = export(service)
    old = service.repository.policy('memories/import-target')
    service.repository.set_policy(replace(old, version='new-scope', classification='secret'), old.version, 'test')
    assert not preview(service, value)['ok']
    value = export(service)
    del value['references']
    assert not preview(service, value)['ok']


def test_reference_only_history_is_encrypted(service):
    seed(service)
    seed(service, 'source-private-label')
    value = export(service)
    value['references'] = [ref(service, 'source-private-label')]
    token = preview(service, value)['result']['token']
    assert call(service, 'records.advanced_commit', {'token': token})['ok']
    with service.store.db() as db:
        raw = db.execute('SELECT value FROM state WHERE key=?', ('record-import-history:' + token,)).fetchone()[0]
    assert 'source-private-label' not in str(raw)
    assert service.store.state('record-import-history:' + token)['after']['sources'] == ['memories/source-private-label']


def test_source_metadata_boolean_and_number_are_distinct_changes(service):
    old = seed(service, kind='knowledge')
    data = {**old['data'], 'source': {'verified': False}}
    service.store.write('knowledge', data, old['version'], 'test', 'import-target')
    value = export(service, kind='knowledge')
    value['data']['source']['verified'] = 0
    p = preview(service, value)['result']
    assert p['changed'] and p['changes'][0]['field'] == 'source'
    assert call(service, 'records.advanced_commit', {'token': p['token']})['ok']
    assert type(service.store.read('knowledge', 'import-target')['data']['source']['verified']) is int


@pytest.mark.parametrize('content', ['中文🙂𠮷', '\ud800'])
def test_json_encoded_file_handles_nonbmp_or_rejects_invalid_unicode_without_breaking_management(service, content):
    seed(service)
    value = export(service)
    value['data']['content'] = content
    response = call(service, 'records.advanced_preview', {'kind': 'memories', 'identifier': 'import-target',
        'expected_version': value['record_version'], 'text': json.dumps(value)})
    if content == '\ud800':
        assert not response['ok'] and response['error'] == 'yaml_import'
    else:
        assert response['ok'], response
        result = call(service, 'records.advanced_commit', {'token': response['result']['token']})
        assert result['ok'] and service.store.read('memories', 'import-target')['data']['content'] == content
    assert call(service, 'session.status')['ok']
