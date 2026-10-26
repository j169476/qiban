"""Offline regressions for startup errors, removed hours and connector navigation."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from bili_assistant.storage import Store
from companion_core.errors import AssistantError
from companion_qq import config as qq_config
from companion_qq.module import QQModule
from companion_wechat.module import WeChatModule
from companion_wechat.native.bridge import validate_build
from companion_wechat.native.protocol import ProbeFailure


@pytest.mark.parametrize('failure,code', [
    (ProbeFailure('bridge_build_changed'), 'bridge_build_changed'),
    (ProbeFailure('private-path-or-account'), 'wechat_native_preflight_failed'),
    (FileNotFoundError('private-path-or-account'), 'wechat_native_preflight_failed'),
    (ValueError('private-path-or-account'), 'wechat_native_preflight_failed'),
])
@pytest.mark.parametrize('during_factory', [False, True])
def test_wechat_preflight_is_sanitized_and_cleans_up(failure, code, during_factory, tmp_path):
    closed = []

    def fail(*args):
        raise failure

    delegate = SimpleNamespace(bind_runtime=lambda _: None, validate_start=fail,
                               session=SimpleNamespace(close=lambda: closed.append(True)))
    module = WeChatModule(None, tmp_path, 'synthetic', factory=fail if during_factory else lambda: delegate)
    with pytest.raises(AssistantError) as error:
        module.validate_start()
    assert error.value.code == code
    assert 'private-path-or-account' not in json.dumps(error.value.as_dict())
    assert module.delegate is None
    assert closed == ([] if during_factory else [True])


@pytest.mark.parametrize('manifest', [None, '{broken', '\ufffd'])
def test_invalid_native_build_files_return_only_stable_codes(tmp_path, manifest):
    dll = tmp_path / 'private-path.dll'
    if manifest is not None:
        dll.write_bytes(b'not-a-valid-dll')
        dll.with_suffix('.json').write_text(manifest, encoding='utf-8')
    with pytest.raises(ProbeFailure) as error:
        validate_build(dll)
    assert str(error.value) in {'bridge_build_missing', 'bridge_build_invalid'}


def test_default_has_no_hours_and_legacy_hours_are_optional_and_ignored(tmp_path):
    store = Store(tmp_path / 'data')
    store.initialize()
    saved = store.read('policy')
    assert 'active_hours' not in saved['data']
    legacy = {**saved['data'], 'active_hours': {'start': '02:00', 'end': '02:00'}}
    store.write('policy', legacy, saved['version'], 'synthetic')
    assert store.validate_all()['ok']


def test_qq_new_provider_is_not_a_retry_and_real_disconnect_is_preserved(tmp_path):
    module = QQModule(SimpleNamespace(state=lambda *args: []), tmp_path)
    module.provider = object()
    assert module.runtime_status()['state'] == 'new'
    module.failures, module.code = 1, 'qq_provider_unavailable'
    assert module.runtime_status()['state'] == 'retrying'
    module.ready, module.code = True, ''
    assert module.runtime_status()['state'] == 'connected'


def test_qq_management_link_uses_local_webui_port_and_never_token(tmp_path):
    assert qq_config.management_url(tmp_path) == 'http://127.0.0.1:6099/webui'
    path = tmp_path / '.dev/qq/runtime/config/webui.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'host': '0.0.0.0', 'port': 6109, 'token': 'private-token'}))
    assert qq_config.management_url(tmp_path) == 'http://127.0.0.1:6109/webui'
    for value in ({'port': 80}, {'port': True}, {'port': '6099'}, {'port': 6099, 'host': 'example.com'}):
        path.write_text(json.dumps(value))
        assert qq_config.management_url(tmp_path) is None
    path.write_text('{broken')
    assert qq_config.management_url(tmp_path) is None


@pytest.mark.asyncio
async def test_bili_full_cycle_at_night_still_enforces_daily_quota(role, monkeypatch):
    from test_cognition import event
    from test_delivery import patch_policy, setup_scheduler

    now = role.clock()
    role.clock = lambda: now + timedelta(hours=12)
    patch_policy(role, lambda p: p.update(active_hours={'start': '09:00', 'end': '23:00'}))
    patch_policy(role, lambda p: p['actions']['reply'].update(daily_limit=1))
    scheduler, calls = setup_scheduler(role, monkeypatch)
    await scheduler.cycle()
    assert len(role.adapter.sent) == 1 and len(calls) == 1
    role.adapter.events = [event(role, 'second')]
    await role.inbox()
    action = role.prepare('reply', {'event_id': 'second', 'text': 'must be quota blocked'})
    with pytest.raises(AssistantError) as error:
        await role.execute(action['id'])
    assert error.value.code == 'quota_exhausted'
    assert len(role.adapter.sent) == 1


@pytest.mark.parametrize('state,owner,allowed', [
    ('deferred', 'role', True), ('deferred', 'other', False),
    ('completed', 'role', False), ('uncertain', 'role', False),
])
def test_old_bili_hours_wait_keeps_owner_and_send_receipt_boundaries(role, state, owner, allowed):
    from bili_assistant import delivery

    target = {'id': 'old-hours', 'received': role.clock().isoformat()}
    role.store.set_state(delivery.key('123', target['id']), {
        'owner': owner, 'status': state, 'reason': 'outside_hours', 'attempts': 1,
        'due_at': (role.clock() + timedelta(hours=12)).isoformat(),
        'config_versions': delivery.config_versions(role.store),
    })
    assert delivery.eligible(role, target) is allowed
