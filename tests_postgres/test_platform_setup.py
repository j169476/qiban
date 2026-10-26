import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations
from companion_service.platform_setup import merge_enrollment
from tests_postgres.test_web import client, login
from tests_postgres.test_web import service as service
from tests_wechat.test_db_source import Harness


@pytest.fixture
def setup(service, tmp_path):
    service.host.project = tmp_path
    return service.setup


def test_setup_status_does_not_connect_and_secret_is_never_returned(setup, monkeypatch):
    saved = {}
    monkeypatch.setattr('keyring.set_password', lambda service, key, value: saved.update(value=value))
    monkeypatch.setattr('keyring.get_password', lambda *args: saved.get('value'))
    assert not setup.status('qq')['configured']
    settings = dict(account='10001', private_targets=['10002'], groups=['20001'],
                    http='http://127.0.0.1:3000', ws='ws://127.0.0.1:3001', token='synthetic-secret-12345')
    setup.save('qq', settings)
    assert setup.status('qq')['token_saved']
    assert settings['token'] not in json.dumps(setup.status('qq'))
    assert settings['token'] not in (setup.project / '.dev/qq/module.json').read_text()
    setup.save('qq', {**settings, 'private_targets': ['10003', '10002'], 'token': ''})
    assert setup.options('qq')['private_targets'] == ['10002', '10003']
    assert ReplyAuthorizations(setup.store).records() == []


def test_setup_malformed_existing_config_is_reported_without_breaking_dashboard(setup):
    path = setup.project / '.dev/wechat/module.json'
    path.parent.mkdir(parents=True)
    path.write_text('{broken')
    assert setup.status('wechat')['configuration_error']
    assert setup.status('wechat')['version']


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["qq", "wechat"])
async def test_dependency_prepare_reuses_verified_configured_environment(setup, monkeypatch, platform):
    python, dll = setup.project / 'custom-python.exe', setup.project / 'custom.dll'
    python.write_bytes(b'synthetic')
    monkeypatch.setattr(setup, 'options', lambda _: {'database_python': python, 'native_dll': dll})
    checked, commands = [], []
    monkeypatch.setattr('companion_wechat.native.bridge.validate_build', lambda path: checked.append(path))

    async def command(args, **kwargs):
        commands.append(args)
        assert args[1] == '-c'  # No pip, compiler, client, or network call.

    monkeypatch.setattr(setup, 'command', command)
    result = await setup.prepare(platform)
    assert result['dependencies'] == 'reused'
    if platform == 'wechat':
        assert result['native'] == 'reused' and checked == [dll]
        assert commands[0][0] == str(python)
    assert len(commands) == 1
    assert ReplyAuthorizations(setup.store).records() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["dependencies_install", "native_build", "native_verify", None])
async def test_dependency_prepare_repairs_missing_parts_and_tracks_stage(setup, monkeypatch, broken):
    python, dll = setup.project / 'custom-python.exe', setup.project / 'custom.dll'
    python.write_bytes(b'synthetic')
    monkeypatch.setattr(setup, 'options', lambda _: {'database_python': python, 'native_dll': dll})
    checks = 0
    builds = 0

    def validate(path):
        nonlocal builds
        builds += 1
        if builds == 1 or broken == 'native_verify':
            raise ValueError('synthetic invalid build')

    async def command(args, **kwargs):
        nonlocal checks
        stage = setup.store.state('platform-setup:wechat')['stage']
        if args[1] == '-c':
            checks += 1
            if checks == 1:
                raise AssistantError('setup_dependency_failed', 'synthetic absent dependency')
        elif stage == broken:
            raise AssistantError('setup_dependency_failed', 'synthetic offline install/build')

    monkeypatch.setattr('companion_wechat.native.bridge.validate_build', validate)
    monkeypatch.setattr(setup, 'command', command)
    if broken:
        with pytest.raises(AssistantError):
            await setup.prepare('wechat')
        assert setup.store.state('platform-setup:wechat')['stage'] == broken
    else:
        assert await setup.prepare('wechat') == {'dependencies': 'installed', 'native': 'built'}
        assert checks == 2 and builds == 2


def test_setup_web_route_and_backups_remain_independent(setup):
    admin = client(setup.service, 'admin')
    headers = login(admin, setup.service, 'admin')
    payload = {'operation': 'platform_setup_status', 'arguments': {'platform': 'qq'}}
    assert admin.post('/operate', json=payload).status_code == 403
    response = admin.post('/operate', json=payload, headers=headers)
    assert response.status_code == 200 and response.json()['platform'] == 'qq'
    assert admin.post('/operate', json={'operation': 'backup_list', 'arguments': {}}, headers=headers).status_code == 200


def test_wechat_setup_more_than_two_targets_round_trips_without_granting(setup, tmp_path):
    settings = {'account_dirs': [str(tmp_path)], 'targets': [
        {'kind': 'private', 'hint': 'wxid_alice'}, {'kind': 'private', 'hint': 'wxid_bob'},
        {'kind': 'group', 'hint': '123@chatroom'}, {'kind': 'group', 'hint': '456@chatroom'}]}
    setup.save('wechat', settings)
    version = setup.revision('wechat')
    setup.save('wechat', {**settings, 'targets': [{'kind': 'private', 'hint': 'wxid_carol'}]})
    assert setup.revision('wechat') != version
    assert len(setup.status('wechat')['configuration']['target_hints']) == 5
    assert ReplyAuthorizations(setup.store).records() == []


@pytest.mark.parametrize("echo_legacy", [False, True])
def test_wechat_legacy_names_are_not_appended_as_new_ids(setup, tmp_path, echo_legacy):
    from companion_core.contracts import stable_key
    from companion_wechat.collector import collector_account

    setup.service.installation_id = 'synthetic-installation'
    lab = Harness(tmp_path)
    try:
        with lab.session() as source:
            snapshot = source.snapshot()
        key = 'wechat-db-enrollment:' + stable_key('logical-account', collector_account(setup.service.installation_id))
        setup.store.set_state(key, snapshot)
        settings = {'account_dirs': [str(lab.account)], 'targets': [{'kind': 'private', 'hint': 'synthetic_owner'}]}
        setup.save('wechat', settings)
        options = setup.options('wechat')
        options['targets'].write_text(json.dumps({'automatic_sending_enabled': False,
            'private': {'wechat_id_hint': 'synthetic_owner'}, 'group': {'window_title_hint': '旧家庭群名'}}))
        additions = ([{'kind': 'private', 'hint': 'synthetic_owner'}, {'kind': 'group', 'hint': '旧家庭群名'},
                      {'kind': 'private', 'hint': 'wxid_new'}] if echo_legacy else [])
        setup.save('wechat', {**settings, 'targets': additions})
        targets = setup.status('wechat')['configuration']['target_hints']
        assert {'kind': 'group', 'hint': 'synthetic_group@chatroom'} in targets
        assert {'kind': 'private', 'hint': 'synthetic_owner'} in targets
        assert len(targets) == (3 if echo_legacy else 2)
        assert all(t['hint'] != '旧家庭群名' for t in targets)
        assert setup.store.state(key) == snapshot
        setup.save('wechat', {**settings, 'targets': []})
        assert setup.status('wechat')['configuration']['target_hints'] == targets
        assert ReplyAuthorizations(setup.store).records() == []
    finally:
        lab.close()


def test_setup_job_rechecks_revision_and_clears_busy_on_reload_failure(setup):
    called = []

    async def detach(platform):
        called.append(platform)

    def attach(platform):
        raise RuntimeError('must never expose raw error')

    setup.service.platform_runs = SimpleNamespace(detach=detach, attach_configured=attach)
    setup.running.add('qq')
    asyncio.run(setup._job(SimpleNamespace(actor=None), 'qq', 'save', {}, 'stale'))
    assert called == ['qq'] and not setup.running and not setup.active_tasks
    state = setup.store.state('platform-setup:qq')
    assert state['status'] == 'failed' and state['error'] == 'edit_conflict'
    assert state['reload_error'] == 'setup_reload_failed'


def test_enrollment_merge_keeps_old_positions_and_new_target_baseline(tmp_path):
    from companion_wechat.db_probe import Target

    lab = Harness(tmp_path)
    try:
        with lab.session() as source:
            old = source.snapshot()
            lab.insert(1)
            old['checkpoint'] = source.read(old['checkpoint'])['next_checkpoint']
        lab.targets.append(Target('new_contact', 'wxid_new'))
        lab.add_shard('message_1.db')
        with lab.session() as source:
            fresh = source.snapshot()
        merged = merge_enrollment(old, fresh)
        assert len(merged['enrollment']['targets']) == 3
        assert merged['enrollment']['epoch'] == old['enrollment']['epoch']
        for key, position in old['checkpoint']['sources'].items():
            assert merged['checkpoint']['sources'][key] == position
        assert 'message/message_1.db|new_contact' in merged['checkpoint']['sources']
        wrong = copy.deepcopy(fresh)
        wrong['checkpoint']['sources']['message/message_0.db|owner_main']['salt'] = 'ff' * 32
        with pytest.raises(AssistantError):
            merge_enrollment(old, wrong)
    finally:
        lab.close()


def test_wechat_probe_pins_pid_not_process_identity_tuple(setup, tmp_path, monkeypatch):
    from companion_core.contracts import stable_key
    from companion_wechat.collector import collector_account

    lab = Harness(tmp_path)
    setup.service.installation_id = 'synthetic-installation'
    try:
        with lab.session() as source:
            snapshot = source.snapshot()
        key = 'wechat-db-enrollment:' + stable_key('logical-account', collector_account(setup.service.installation_id))
        setup.store.set_state(key, snapshot)
        setup.save('wechat', {'account_dirs': [str(lab.account)], 'targets': [{'kind': 'private', 'hint': 'synthetic_owner'}]})
        monkeypatch.setattr('companion_wechat.module.ReconnectingTransport._select', lambda _: (12345, 999999))
        closed = []

        class Worker:
            def __init__(self, **kwargs):
                pass

            async def request(self, operation, **kwargs):
                assert operation == 'open' and kwargs['config']['client_pid'] == 12345
                return snapshot

            async def close(self):
                closed.append(True)

        monkeypatch.setattr('companion_wechat.receiver_transport.SubprocessTransport', Worker)
        result = asyncio.run(setup.probe('wechat'))
        assert result['verified'] and result['messages_sent'] == 0 and closed == [True]
        assert setup.store.state(key) == snapshot
        assert ReplyAuthorizations(setup.store).records() == []
    finally:
        lab.close()
