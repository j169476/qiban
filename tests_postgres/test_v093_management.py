from types import SimpleNamespace

from companion_core.reply_authorization import ReplyScope, ReplyTarget
from companion_core.reply_policy import Participation
from tests_postgres.test_reply_authorization import service as service
from tests_postgres.test_web import ADMIN, client, login


def test_object_quota_web_auth_cas_defaults_and_isolation(service):
    first = ReplyScope('qq', '10001', 'private', '10002')
    second = ReplyScope('qq', '10001', 'private', '10003')
    adapter = SimpleNamespace(reply_targets=lambda: tuple(ReplyTarget(s, 'test', 'account') for s in (first, second)))
    service.platform_runs.cores['qq'] = SimpleNamespace(platforms=SimpleNamespace(all=lambda: [adapter]))
    admin = client(service, 'admin')
    headers = login(admin, service, 'admin')

    def operation(name, args=None, h=headers):
        return admin.post('/operate', json={'operation': name, 'arguments': args or {}}, headers=h)

    current = operation('reply_policy_list').json()['contacts'][0]
    args = {'scope_id': current['scope_id'], 'expected_version': current['version'],
            'changes': {'private_daily_replies': 12}}
    assert operation('reply_policy_object_set', args, h={'origin': ADMIN}).status_code == 403
    saved = operation('reply_policy_object_set', args)
    assert saved.status_code == 200 and not saved.json()['platform_paused'] and not saved.json()['role_started']
    assert operation('reply_policy_object_set', args).status_code == 409
    listing = operation('reply_policy_list').json()['contacts']
    overridden = next(row for row in listing if row['scope_id'] == current['scope_id'])
    other = next(row for row in listing if row['scope_id'] != current['scope_id'])
    assert overridden['settings']['private_daily_replies'] == 12 and not overridden['inherited']
    assert other['settings']['private_daily_replies'] == 200 and other['inherited']
    scope = first if current['scope_id'] == first.id else second
    key, _ = Participation(service.store).usage(scope)
    service.store.set_state(key, {'confirmed': 4, 'reserved': 1, 'uncertain': 1})
    assert operation('reply_policy_object_reset', {'scope_id': current['scope_id'],
        'expected_version': overridden['version']}).status_code == 200
    assert Participation(service.store).usage(scope)[1] == {'confirmed': 4, 'reserved': 1, 'uncertain': 1}
    assert operation('reply_policy_object_reset', {'scope_id': 'unknown', 'expected_version': None}).status_code == 403
    role = client(service, 'role')
    assert role.post('/operate', json={'operation': 'reply_policy_object_set', 'arguments': args},
                     headers=login(role, service, 'role')).status_code == 404


def test_growth_recovery_web_requires_preview_paused_and_same_session(service):
    admin = client(service, 'admin')
    headers = login(admin, service, 'admin')

    def operation(name, args=None, h=headers):
        return admin.post('/operate', json={'operation': name, 'arguments': args or {}}, headers=h)

    assert operation('public_growth_preview', h={'origin': ADMIN}).status_code == 403
    service.store.set_state('core-run:bilibili', {'status': 'active'})
    assert operation('public_growth_preview').status_code == 403
    service.store.set_state('core-run:bilibili', {'status': 'paused'})
    preview = operation('public_growth_preview')
    assert preview.status_code == 200, preview.text
    token = preview.json()['token']
    assert operation('public_growth_rebuild', {'token': 'wrong'}).status_code == 409
    result = operation('public_growth_rebuild', {'token': token})
    assert result.status_code == 200, result.text
    assert result.json()['requires_explicit_start']
    assert service.store.state(result.json()['archive']) is not None
    assert service.store.state('core-run:bilibili')['status'] == 'paused'
    assert operation('public_growth_rebuild', {'token': token}).status_code == 409


def test_bilingual_manual_is_available_without_starting_service(service):
    for kind in ('admin', 'role'):
        response = client(service, kind).get('/manual')
        assert response.status_code == 200
        assert 'data-manual-language="zh"' in response.text and 'data-manual-language="en"' in response.text
    assert service.status()['status'] == 'paused'
