"""Real encrypted ledger and management commands; entirely synthetic notifications."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from bili_assistant.health import notification_status
from companion_core.errors import AssistantError
from companion_service import faults
from companion_service.monitoring import snapshot
from tests_postgres.test_web import service as service


@pytest.fixture
def notices(service):
    sent = []
    service.control_verifier = lambda thread: None
    service.host.notification_ready = lambda thread: None
    service.host.notify_admin = lambda thread, text, key: sent.append((thread, text, key)) or {"turn_id": key}
    config = service.store.read('companion')
    service.store.write('companion', {**config['data'], 'failure_notifications': 'admin_once',
                        'management_thread_id': 'old-admin'}, config['version'], 'test')

    def bind(thread):
        return service.control('synthetic-bridge-secret', thread, 'management_open', {})

    bind('old-admin')
    bind('admin')
    return SimpleNamespace(service=service, store=service.store, sent=sent, bind=bind)


def enqueue(env, generation='synthetic-fault', *, run_key='core-run', owner='admin'):
    env.store.set_state(run_key, {'status': 'paused', 'generation': generation, 'owner': owner})
    faults.record(env.service, 'supervisor_lost', run_key=run_key)
    return 'fault:' + generation


@pytest.mark.parametrize('run_key', ['core-run', 'core-run:bilibili', 'core-run:qq', 'core-run:wechat'])
def test_all_platforms_notify_bound_admin_once_even_when_owner_matches(notices, run_key):
    env = notices
    key = enqueue(env, run_key=run_key)
    policy = env.store.read('policy')
    settings = env.store.read('companion')
    env.store.set_state('model-budget:synthetic', {'used': 62})
    assert faults.deliver(env.service) == ['synthetic-fault']
    assert faults.deliver(env.service) == []
    assert [item[0] for item in env.sent] == ['admin']
    assert env.store.state(key)['destination'] == 'admin'
    assert env.store.state(key)['notification'] == 'delivered'
    assert env.store.read('policy') == policy and env.store.read('companion') == settings
    assert env.store.state('model-budget:synthetic') == {'used': 62}
    assert env.store.state(run_key)['status'] == 'paused'
    with env.store.db() as db:
        value = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()[0]
    assert value.startswith('enc1:')


def test_rebinding_moves_pending_faults_and_report_without_replaying_delivered(notices):
    env = notices
    first = enqueue(env, 'already-delivered')
    assert faults.deliver(env.service) == ['already-delivered']
    queued = enqueue(env, 'pending')
    config = env.store.read('companion')
    assert env.bind('new-admin') == {'bound': True, 'role_started': False}
    assert env.store.state('service-admin-thread') == env.store.state('management_thread') == 'new-admin'
    expected = {'mode': 'admin_once', 'routing': 'current_management', 'destination': 'new-admin'}
    assert snapshot(env.service)['notifications'] == expected
    assert env.store.read('companion') == config  # No stale duplicate config field is a route.
    assert len(env.sent) == 1  # Reading status never dispatches anything.
    assert faults.deliver(env.service) == ['pending']
    assert [item[0] for item in env.sent] == ['admin', 'new-admin']
    assert env.store.state(first)['destination'] == 'admin'
    assert env.store.state(queued)['destination'] == 'new-admin'
    assert faults.deliver(env.service) == []


@pytest.mark.parametrize('binding', [None, '', 'missing-admin', 'role-control'])
def test_invalid_current_binding_never_falls_back_to_old_admin(notices, binding):
    env = notices
    key = enqueue(env)
    with env.store.db() as db:
        db.execute('INSERT INTO sessions VALUES(?,?)', ('role-control', 'role'))
    env.store.set_state('service-admin-thread', binding)
    assert notification_status(env.store)['destination'] is None
    assert faults.deliver(env.service) == [] and not env.sent
    assert env.store.state(key)['notification'] == 'queued'


def test_service_binding_is_authoritative_over_legacy_binding_and_config(notices):
    env = notices
    enqueue(env)
    env.store.set_state('management_thread', 'old-admin')
    assert faults.deliver(env.service) == ['synthetic-fault']
    assert [item[0] for item in env.sent] == ['admin']


def test_service_with_no_binding_record_never_uses_imported_legacy_binding(notices):
    env = notices
    key = enqueue(env)
    with env.store.db() as db:
        db.execute("DELETE FROM state WHERE key='service-admin-thread'")
    assert notification_status(env.store)['destination'] is None
    assert faults.deliver(env.service) == [] and not env.sent
    assert env.store.state(key)['notification'] == 'queued'


@pytest.mark.parametrize('reason', ['chat_archived', 'admin_busy', 'host_unavailable'])
def test_unavailable_admin_leaves_fault_queued_until_available(notices, reason):
    env = notices
    key = enqueue(env)

    def unavailable(thread):
        raise AssistantError(reason, 'private synthetic diagnostic')

    env.service.host.notification_ready = unavailable
    assert faults.deliver(env.service) == [] and not env.sent
    assert env.store.state(key)['notification'] == 'queued'
    env.service.host.notification_ready = lambda _: None
    assert faults.deliver(env.service) == ['synthetic-fault']
    assert 'private synthetic diagnostic' not in json.dumps(env.store.state(key))


def test_off_setting_and_revocation_during_host_check_never_dispatch(notices):
    env = notices
    key = enqueue(env)

    def revoke(thread):
        config = env.store.read('companion')
        env.store.write('companion', {**config['data'], 'failure_notifications': 'off'}, config['version'], 'test')

    env.service.host.notification_ready = revoke
    assert faults.deliver(env.service) == [] and not env.sent
    env.service.host.notification_ready = lambda _: pytest.fail('off must not query host')
    assert faults.deliver(env.service) == [] and not env.sent
    assert env.store.state(key)['notification'] == 'queued'


def test_rebind_during_readiness_check_does_not_reserve_old_target(notices):
    env = notices
    key = enqueue(env)
    env.service.host.notification_ready = lambda _: env.bind('new-admin')
    assert faults.deliver(env.service) == [] and not env.sent
    assert env.store.state(key)['notification'] == 'queued'
    env.service.host.notification_ready = lambda _: None
    assert faults.deliver(env.service) == ['synthetic-fault']
    assert env.sent[0][0] == 'new-admin'


def test_each_queued_fault_re_resolves_binding_within_same_delivery_pass(notices):
    env = notices
    enqueue(env, 'first')
    enqueue(env, 'second')

    def notify(thread, text, key):
        env.sent.append((thread, text, key))
        env.bind('new-admin')
        return {'turn_id': key}

    env.service.host.notify_admin = notify
    assert faults.deliver(env.service) == ['first', 'second']
    assert [item[0] for item in env.sent] == ['admin', 'new-admin']
    assert faults.deliver(env.service) == []


def test_lost_response_stays_uncertain_after_rebind_without_resending(notices):
    env = notices
    key = enqueue(env)

    def notify(*args):
        env.sent.append(args)
        raise TimeoutError('secret-token-not-for-records')

    env.service.host.notify_admin = notify
    assert faults.deliver(env.service) == []
    assert env.store.state(key)['notification'] == 'uncertain'
    env.bind('new-admin')
    assert faults.deliver(env.service) == [] and len(env.sent) == 1
    assert env.store.state(key)['destination'] == 'admin'
    assert 'secret-token-not-for-records' not in json.dumps(env.store.state(key))


def test_concurrent_notifiers_and_rebind_cannot_duplicate_reserved_delivery(notices):
    env = notices
    key = enqueue(env)
    entered, release = threading.Event(), threading.Event()

    def notify(*args):
        env.sent.append(args)
        entered.set()
        assert release.wait(10)
        return {'turn_id': 'synthetic-turn'}

    env.service.host.notify_admin = notify
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(faults.deliver, env.service)
        try:
            assert entered.wait(10)
            assert env.store.state(key)['notification'] == 'dispatching'
            env.bind('new-admin')  # A slow external receipt never blocks management.
            assert faults.deliver(env.service) == []
        finally:
            release.set()
        assert task.result(timeout=10) == ['synthetic-fault']
    assert faults.deliver(env.service) == [] and len(env.sent) == 1
    assert env.store.state(key)['destination'] == 'admin'
