"""Real web/core/PostgreSQL paths, synthetic adapters and offline native artifacts."""
import asyncio
import json
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from companion_core.reply_authorization import ReplyAuthorizations
from companion_service.service import Service
from companion_wechat.module import WeChatModule
from companion_wechat.native.bridge import validate_build
from tests_postgres.test_bili_web_lifecycle import pg_profile as pg_profile
from tests_postgres.test_platform_isolation import call, prepare
from tests_postgres.test_runtime import Model
from tests_postgres.test_web import PASSWORD, Host, client, login


@pytest.mark.parametrize('condition,code', [
    ('missing', 'bridge_build_missing'), ('manifest_missing', 'bridge_build_missing'),
    ('invalid', 'bridge_build_invalid'), ('mismatch', 'bridge_build_changed'),
])
def test_web_start_returns_sanitized_native_error_and_stays_paused(pg_store, tmp_path, condition, code):
    dll = tmp_path / 'private-account.dll'
    if condition != 'missing':
        dll.write_bytes(b'synthetic-bridge')
    if condition == 'invalid':
        dll.with_suffix('.json').write_text('{private-account', encoding='utf-8')
    if condition == 'mismatch':
        dll.with_suffix('.json').write_text(json.dumps({'sources': 'old', 'sha256': 'old'}))
    closed = []
    delegate = SimpleNamespace(bind_runtime=lambda _: None, validate_start=lambda: validate_build(dll),
                               session=SimpleNamespace(close=lambda: closed.append(True)))
    module = WeChatModule(pg_store, tmp_path, 'synthetic', factory=lambda: delegate)
    service = Service(pg_store, Model(), Host(), adapters=[module], bridge_secret='synthetic-bridge-secret')
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    service.control_verifier = lambda _: None
    service.control('synthetic-bridge-secret', 'admin', 'management_open', {})
    web = client(service, 'admin')
    headers = login(web, service, 'admin')
    response = web.post('/operate', headers=headers, json={'operation': 'platform_control', 'arguments': {
        'platform': 'wechat', 'action': 'start', 'expected_generation': None, 'options': {}}})
    assert response.status_code == 403
    assert response.headers['content-type'].startswith('application/json')
    assert response.json()['error'] == code
    assert '接入向导' in response.json()['message']
    assert 'private-account' not in response.text and str(tmp_path) not in response.text
    assert service.platform_runs.status('wechat')['status'] == 'paused'
    assert service.status()['status'] == 'paused'
    assert module.delegate is None and closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['bilibili', 'qq', 'wechat'])
async def test_legacy_rest_hours_do_not_block_started_platform_polling(pg_store, platform):
    policy = pg_store.read('policy')
    pg_store.write('policy', {**policy['data'], 'active_hours': {'start': '09:00', 'end': '23:00'}},
                   policy['version'], 'synthetic')
    service = prepare(pg_store)
    core = service.platform_runs.cores[platform]
    service.core.clock = core.clock = lambda: datetime(2026, 10, 26, 16, 30, tzinfo=timezone.utc)
    alias = 'bili' if platform == 'bilibili' else platform
    call(service, platform + '-owner', alias + '_start')
    original_run = pg_store.state(core.run_key)
    old_policy = pg_store.read('policy')
    permissions = ReplyAuthorizations(pg_store).records()
    adapter = core.platforms.all()[0]
    polled = []
    adapter.runtime_status = lambda: {'state': 'connected' if polled else 'new', 'code': ''}

    async def poll():
        polled.append(True)
        service.shutdown.set()
        return []

    adapter.runtime_poll = poll
    assert service.platform_runs.status(platform)['connection']['state'] == 'new'
    await asyncio.wait_for(service.platform_runs.work(platform), 4)
    status = service.platform_runs.status(platform)
    assert polled == [True] and status['connection']['state'] == 'connected'
    assert 'waiting_for_schedule' not in status and status['reason'] != 'outside_active_hours'
    assert pg_store.state(core.run_key) == original_run
    assert pg_store.read('policy') == old_policy
    assert ReplyAuthorizations(pg_store).records() == permissions
    assert not adapter.sent and not core.decisions.model.contexts
    call(service, platform + '-owner', alias + '_stop')
    assert service.platform_runs.status(platform)['status'] == 'paused'


@pytest.mark.parametrize('detail,expected', [
    ({'receiver': {'state': 'new', 'code': ''}}, {'state': 'new', 'code': ''}),
    ({'receiver': {'state': 'retrying', 'code': 'client_not_running'}},
     {'state': 'retrying', 'code': 'client_not_running'}),
    ({'receiver': {'state': 'blocked', 'code': 'source_unavailable'}},
     {'state': 'blocked', 'code': 'source_unavailable'}),
    ({'receiver': {'state': 'connected'}, 'native': {'code': 'client_restart_required'}},
     {'state': 'blocked', 'code': 'client_restart_required'}),
    ({'receiver': {'state': 'connected'}, 'native_uncertain': True},
     {'state': 'uncertain', 'code': 'send_unconfirmed'}),
])
def test_connection_details_preserve_real_faults_and_uncertain_sends(pg_store, detail, expected):
    service = prepare(pg_store)
    adapter = service.platform_runs.cores['wechat'].platforms.all()[0]
    adapter.runtime_status = lambda: detail
    assert service.platform_runs.status('wechat')['connection'] == expected


def test_qq_card_and_setup_share_local_management_url_without_mutating_run(pg_store, tmp_path):
    service = prepare(pg_store)
    service.host.project = tmp_path
    path = tmp_path / '.dev/qq/runtime/config/webui.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'host': '127.0.0.1', 'port': 6209, 'token': 'private-token'}))
    card = service.platform_runs.status('qq')
    wizard = service.setup.status('qq')
    assert card['management_url'] == wizard['login_url'] == 'http://127.0.0.1:6209/webui'
    assert 'private-token' not in json.dumps([card, wizard])
    assert card['status'] == 'paused' and not card['busy']


@pytest.mark.parametrize('state,owner,allowed', [
    ('deferred', 'role', True), ('deferred', 'other', False),
    ('completed', 'role', False),
])
def test_old_hours_wait_wakes_indexed_inbox_but_keeps_delivery_guards(pg_store, state, owner, allowed):
    from companion_core import delivery, events
    from companion_core.contracts import ActorRef, Event

    now = datetime(2026, 10, 26, 16, 30, tzinfo=timezone.utc)
    item = Event('qq', 'bot', 'old-hours', ActorRef('qq', 'user'), 'room', now.isoformat(), 'synthetic')
    version = events.ingest(pg_store, item)['version']
    legacy_versions = {kind: pg_store.read(kind)['version']
                       for kind in ('manifest', 'policy', 'preferences', 'persona', 'companion')}
    legacy_token = sha256(json.dumps(legacy_versions, sort_keys=True).encode()).hexdigest()
    pg_store.set_state(delivery.key(item), {
        'version': version, 'owner': owner, 'status': state, 'reason': 'outside_hours', 'attempts': 1,
        'due_at': '2026-10-27T09:00:00+08:00', 'config_token': legacy_token,
    })
    core = SimpleNamespace(store=pg_store, clock=lambda: now)
    pending = events.pending(pg_store, [['qq', 'bot']], now.isoformat(), owner='role',
                             now=now.isoformat(), config_token=delivery.config_token(pg_store))
    if allowed:
        assert pending == [item]
    assert delivery.eligible(core, item, version, {'owner': 'role'}) is allowed
