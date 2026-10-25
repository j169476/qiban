import secrets

import pytest

from companion_core.errors import AssistantError
from tests_postgres.test_web import PASSWORD, client, login
from tests_postgres.test_web import service as service_fixture

service = service_fixture


def test_password_change_revokes_both_sessions_and_pauses(service):
    admin, role = client(service, 'admin'), client(service, 'role')
    headers = login(admin, service, 'admin')
    login(role, service, 'role')
    service.control('synthetic-bridge-secret', 'control', 'assistant_start', {})
    body = {'old_password': PASSWORD, 'password': 'synthetic-new-password'}
    assert admin.post('/password', json=body, headers={'origin': 'http://127.0.0.1:45180'}).status_code == 403
    assert admin.post('/password', json={**body, 'old_password': 'wrong-password-2026'}, headers=headers).status_code == 403
    assert service.status()['status'] == 'active'
    assert admin.post('/password', json=body, headers=headers).status_code == 200
    assert service.status()['status'] == 'paused'
    assert role.get('/status').status_code == 401
    assert admin.get('/status').status_code == 401
    assert admin.post('/login', json={'password': PASSWORD}, headers=headers).status_code == 403
    assert admin.post('/login', json={'password': body['password']}, headers=headers).status_code == 200


def test_password_reset_only_bound_bridge_single_use_and_expiry(service):
    admin = client(service, 'admin')
    headers = login(admin, service, 'admin')
    with pytest.raises(AssistantError, match='绑定'):
        service.control('synthetic-bridge-secret', 'control', 'password_reset_ticket', {})
    service.control('synthetic-bridge-secret', 'admin', 'management_open', {})
    assert admin.post('/control', json={'thread': 'admin', 'operation': 'password_reset_ticket'}, headers=headers).status_code == 403
    ticket = service.control('synthetic-bridge-secret', 'admin', 'password_reset_ticket', {})['ticket']
    args = {'ticket': ticket, 'password': 'synthetic-reset-password'}
    assert admin.post('/password-reset', json={**args, 'ticket': secrets.token_urlsafe(32)}, headers=headers).status_code == 403
    assert admin.post('/password-reset', json=args, headers=headers).status_code == 200
    assert admin.post('/password-reset', json=args, headers=headers).status_code == 403
    assert service.status()['status'] == 'paused'
    ticket = service.sessions.reset_ticket()
    state = service.store.state('web-reset')
    service.store.set_state('web-reset', {**state, 'expires': 0})
    with pytest.raises(AssistantError, match='过期'):
        service.sessions.reset_password(ticket, 'another-synthetic-password')


def memory(service, key, source=None):
    store = service.store
    store.write('memories', {'schema_version': 1, 'id': key, 'kind': 'experience', 'content': 'synthetic original',
        'source': {'type': 'interaction', 'reference': key, 'recorded_at': '2026-10-24T00:00:00Z'},
        'confidence': 'observed', 'tags': [], 'locked': False}, None, 'test', key)
    from companion_core.access import RecordPolicy

    repo = service.sessions.repository
    p = repo.policy('memories/' + key)
    parent = repo.policy('memories/' + source) if source else None
    repo.set_policy(RecordPolicy(p.resource, secrets.token_hex(16), 'public', sources=(parent.resource,) if parent else (),
        source_versions=((parent.resource, parent.version),) if parent else ()), p.version, 'test')


def test_explanation_delete_preview_cas_and_revoke_derivatives(service):
    admin = client(service, 'admin')
    headers = login(admin, service, 'admin')
    memory(service, 'original')
    memory(service, 'derived', 'original')

    def operate(operation, **arguments):
        return admin.post('/operate', json={'operation': operation, 'arguments': arguments}, headers=headers)

    explanation = operate('record_explain', kind='memories', id='derived')
    assert explanation.status_code == 200, explanation.text
    assert explanation.json()['total_nodes'] == 2
    assert explanation.json()['total_issues'] == 0
    preview = operate('delete_preview', kind='memories', id='original').json()
    assert preview['affected_count'] == 2
    memory(service, 'new-dependent', 'original')
    assert operate('record_delete', token=preview['token']).status_code == 409
    assert service.store.exists('memories', 'original')
    preview = operate('delete_preview', kind='memories', id='original').json()
    deleted = operate('record_delete', token=preview['token'])
    assert deleted.status_code == 200, deleted.text
    assert not service.store.exists('memories', 'original')
    assert service.store.exists('memories', 'derived')
    explanation = operate('record_explain', kind='memories', id='derived').json()
    assert any(item['reason'] == 'source_missing_or_changed' for item in explanation['issues'])
    assert operate('record_delete', token=preview['token']).status_code == 403
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM content_history WHERE kind='memories' AND id='original'").fetchone()[0] == 1
    assert operate('delete_preview', kind='persona', id='@config').status_code == 403
