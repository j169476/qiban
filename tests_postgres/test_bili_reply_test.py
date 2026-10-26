"""Real dispatch reservations with synthetic ingress and a fake remote adapter."""
import asyncio
from datetime import timedelta

import pytest

from bili_assistant.connector import normalize
from companion_core.errors import AssistantError
from companion_core.reply_test import attempt_key
from tests_postgres.test_bili_governance import NOW, patch
from tests_postgres.test_bili_web_lifecycle import pg_profile as pg_profile
from tests_postgres.test_bili_web_lifecycle import service as service
from tests_postgres.test_bili_web_lifecycle import start_web

SCOPE = {"account_uid": "123", "target_uid": "11"}


@pytest.fixture
def ready(service):
    core = service.platform_runs.cores['bilibili']
    core.clock = lambda: NOW
    connector = core.platforms.all()[0]
    connector.engine.clock = core.clock
    patch(service.store, 'policy', min_action_interval_seconds=0,
          active_hours={'start': '00:00', 'end': '23:59'})
    patch(service.store, 'companion', memory_maintenance=False, daily_reflection=False,
          knowledge_review=False, media_fallback='off', search_expansion=False, native_tools_enabled=False)
    return service, core, connector


def start(ready, **changes):
    service, core, connector = ready
    options = {'reply_test': SCOPE, 'duration_minutes': 10, **changes}
    start_web(service, **options)
    return service, core, connector


@pytest.mark.asyncio
async def test_only_new_private_target_reaches_model_and_send_then_pauses(ready):
    service, core, connector = start(ready)
    incoming = await core.poll()
    assert [event.actor.user_id for event in incoming] == ['11']
    assert [c.name for c in connector.capabilities()] == ['message.reply']
    assert connector.workflow(core).stages == ()
    generation = core.guard()['generation']
    result = await core.handle(incoming[0])
    assert result['action']['status'] == 'succeeded'
    assert len(connector.engine.adapter.sent) == 1
    sent = connector.engine.adapter.sent[0]
    assert sent[0] == 'dm_reply' and sent[1]['_event']['uid'] == '11'
    assert service.store.state(core.run_key)['reason'] == 'reply_test_complete'
    with service.store.db() as db:
        native = db.execute('SELECT id,status FROM actions WHERE generation=?', (generation,)).fetchall()
        unified = db.execute('SELECT status FROM core_actions WHERE generation=?', (generation,)).fetchall()
    assert len(native) == len(unified) == 1
    assert native[0]['status'] == unified[0]['status'] == 'succeeded'
    assert service.store.state('reply-test-attempt:'+generation) == {'action_id': native[0]['id']}


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['other_uid', 'old', 'public', 'other_account'])
async def test_direct_handle_rejects_outside_scope_before_model(ready, change):
    _, core, connector = start(ready)
    item = dict(connector.engine.adapter.incoming[0])
    if change == 'other_uid':
        item['uid'] = '22'
    elif change == 'old':
        item['received'] = (NOW-timedelta(seconds=1)).isoformat()
    elif change == 'public':
        item['kind'] = 'reply'
        item.update(oid='100', root='200', parent='200')
    event = normalize('999' if change == 'other_account' else '123', item)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Out-of-scope event reached model')
    core.decisions.generate = forbidden
    with pytest.raises(AssistantError) as error:
        await core.handle(event)
    assert error.value.code == 'reply_test_scope'
    assert not connector.engine.adapter.sent
    assert core.guard()['status'] == 'active'


@pytest.mark.asyncio
async def test_native_dispatch_cannot_bypass_target_or_action_limit(ready):
    service, core, connector = start(ready)
    await core.poll()
    engine = connector.engine
    for kind, payload in [('dm_reply', {'event_id': 'dm-22', 'text': 'synthetic'}),
                          ('reply', {'event_id': 'reply-33', 'text': 'synthetic'})]:
        prepared = engine.prepare(kind, payload)
        with pytest.raises(AssistantError) as error:
            await engine.execute(prepared['id'])
        assert error.value.code == 'reply_test_scope'
    assert not engine.adapter.sent
    assert service.store.state(attempt_key(core.guard())) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['like', 'favorite', 'coin', 'comment', 'comment_like'])
async def test_every_other_native_action_is_denied(ready, kind):
    service, core, connector = start(ready)
    await core.poll()
    engine = connector.engine
    policy = service.store.read('policy')['data']
    actions = {k: dict(v) for k,v in policy['actions'].items()}
    actions[kind].update(enabled=True, daily_limit=5)
    patch(service.store, 'policy', actions=actions)
    service.store.set_state('context:BV1234567890',
        {'owner_uid': '11', 'title': 'synthetic', 'coverage': {'kind': 'full'}})
    payload = {'event_id':'reply-33'} if kind=='comment_like' else {'bvid':'BV1234567890'}
    if kind=='coin':
        payload['units']=1
    if kind=='comment':
        payload['text']='synthetic'
    prepared=engine.prepare(kind,payload)
    with pytest.raises(AssistantError) as error:
        await engine.execute(prepared['id'])
    assert error.value.code=='reply_test_scope'
    assert not engine.adapter.sent
    assert service.store.state(attempt_key(core.guard())) is None


@pytest.mark.asyncio
async def test_final_send_guard_rechecks_native_event(ready):
    service, core, connector = start(ready)
    incoming = await core.poll()
    engine = connector.engine

    async def changed(kind, payload, policy, before_send):
        with service.store.db() as db:
            db.execute('UPDATE events SET uid=? WHERE id=? AND account=?', ('22', 'dm-11', '123'))
        before_send()
        engine.adapter.sent.append((kind, payload))
        return {'synthetic': True}

    engine.adapter.perform = changed
    result = await core.handle(incoming[0])
    assert result['action']['status'] == 'cancelled'
    assert not engine.adapter.sent
    assert service.store.state(core.run_key)['status'] == 'paused'


@pytest.mark.asyncio
async def test_two_prepared_actions_cannot_reserve_two_attempts(ready):
    service, core, connector = start(ready)
    engine = connector.engine
    engine.adapter.incoming.append({**engine.adapter.incoming[0], 'id': 'dm-11-second'})
    await core.poll()
    first = engine.prepare('dm_reply', {'event_id': 'dm-11', 'text': 'one'})
    second = engine.prepare('dm_reply', {'event_id': 'dm-11-second', 'text': 'two'})

    async def perform(kind, payload, policy, before_send):
        before_send()
        engine.adapter.sent.append((kind, payload))
        await asyncio.sleep(0)
        return {'synthetic': True}

    engine.adapter.perform = perform
    results = await asyncio.gather(engine.execute(first['id']), engine.execute(second['id']), return_exceptions=True)
    assert len(engine.adapter.sent) == 1
    assert sum(isinstance(r, dict) and r['status']=='succeeded' for r in results) == 1
    assert any(isinstance(r, AssistantError) and r.code == 'reply_test_exhausted' for r in results)
    assert service.store.state(core.run_key)['status'] == 'paused'


@pytest.mark.asyncio
async def test_unknown_result_stops_and_never_retries(ready):
    service, core, connector = start(ready)
    incoming = await core.poll()

    async def uncertain(kind, payload, policy, before_send):
        before_send()
        connector.engine.adapter.sent.append((kind,payload))
        raise OSError('synthetic unknown transport outcome')

    connector.engine.adapter.perform = uncertain
    result = await core.handle(incoming[0])
    assert result['action']['status'] == 'uncertain'
    with pytest.raises(AssistantError):
        await core.handle(incoming[0])
    assert len(connector.engine.adapter.sent) == 1
    assert service.store.state(core.run_key)['status'] == 'paused'


def test_normal_restart_drops_test_scope_and_has_fresh_generation(ready):
    service, core, connector = start(ready)
    previous = core.guard()
    core.stop()
    service.manage_platform('bilibili', 'start', previous['generation'], {})
    run = core.guard()
    assert 'reply_test' not in run and run['generation'] != previous['generation']
    assert any(c.name == 'bilibili.like' for c in connector.capabilities())
    assert service.store.state(attempt_key(run)) is None


def test_duration_expires_without_a_message(ready):
    service, core, _ = start(ready)
    core.clock = lambda: NOW+timedelta(minutes=11)
    with pytest.raises(AssistantError) as error:
        core.guard()
    assert error.value.code == 'duration_complete'
    assert service.store.state(core.run_key)['status'] == 'paused'


def test_late_monitor_failure_does_not_overwrite_intentional_test_end(ready):
    service, core, _ = start(ready)
    generation=core.guard()['generation']
    core.stop('reply_test_complete')
    service.platform_runs.fail('bilibili','run_suspended')
    assert service.store.state(core.run_key)['reason']=='reply_test_complete'
    assert service.store.state('fault:'+generation) is None


@pytest.mark.parametrize('options', [
    {'duration_minutes': None}, {'duration_minutes': 31}, {'read_only': True},
    {'reply_test': {'account_uid': '999', 'target_uid': '11'}},
    {'reply_test': {'account_uid': '123', 'target_uid': '123'}},
    {'reply_test': {'account_uid': '123', 'target_uid': '11', 'extra': 'ignored?'}},
])
def test_invalid_scope_never_activates(ready, options):
    service, core, _ = ready
    with pytest.raises(AssistantError):
        service.manage_platform('bilibili', 'start', None,
            {'reply_test': SCOPE, 'duration_minutes': 10, **options})
    assert service.store.state(core.run_key, {}).get('status') != 'active'
