import secrets

from companion_core.media_budget import DEFAULTS, MediaBudgetSettings
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


def test_settings_persist_cas_dedup_and_admin_lock(service):
    initial = call(service, 'media.budget')['result']
    assert initial == {'limits': DEFAULTS, 'revision': None}
    limits = {**DEFAULTS, 'images': 4, 'audio_seconds': 90}
    request = secrets.token_hex(16)
    args = {'limits': limits, 'expected': None}
    saved = call(service, 'media.budget.configure', args, request_id=request)
    assert saved['ok'] and saved['result']['limits'] == limits
    assert call(service, 'media.budget.configure', args, request_id=request) == saved
    assert call(service, 'media.budget.configure', args)['error'] == 'edit_conflict'
    assert MediaBudgetSettings(service.store).read() == saved['result']
    assert call(service, 'media.list', {'after': '', 'limit': 50})['result']['budget'] == saved['result']
    invalid = {'limits': {**limits, 'audio': True}, 'expected': saved['result']['revision']}
    assert call(service, 'media.budget.configure', invalid)['error'] == 'media_budget_invalid'
    assert MediaBudgetSettings(service.store).read() == saved['result']
    service.enable_lock('test-password-123456')
    service.lock()
    assert call(service, 'media.budget')['error'] == 'desktop_locked'
    assert call(service, 'media.budget.configure', args)['error'] == 'desktop_locked'
