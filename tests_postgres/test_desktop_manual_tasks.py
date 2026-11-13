"""Disposable database / synthetic model checks; no network or real model calls."""
import asyncio
import secrets

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_runtime import PREFERENCES, DesktopRuntime
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service
from tests_postgres.test_desktop_runtime import account_runtime


class Bili:
    uid = '123'
    def _account(self):
        return {'uid': self.uid}
    def close(self):
        pass
    def require_run_account(self, accounts):
        if accounts != [['bilibili', self.uid]]:
            raise AssistantError('account_changed', 'synthetic changed account')


ARGS = dict(bvid='BV1234567890', page_index=0, start_seconds=0, end_seconds=60, mode='text', reason='合成检查')


def prepare(service):
    service.bili = Bili()
    runtime = DesktopRuntime(service, 'manual-test', model=object())
    return runtime, runtime.manual


def submit(manual, nonce=None):
    return manual.submit(manual.manager.session_id, 'video', ARGS, nonce or secrets.token_hex(16))['id']


def done(summary='合成结果'):
    return dict(phase='done', complete=True, payload={'text': summary}, summary=summary)


@pytest.mark.asyncio
async def test_stopped_platform_submit_duplicate_and_private_result(service):
    runtime, manual = prepare(service)
    before = service.store.state(PREFERENCES)
    calls = []
    async def runner(task, previous, **kwargs):
        kwargs['guard']()
        calls.append(previous)
        return done()
    manual.runner = runner
    nonce = secrets.token_hex(16)
    identifier = submit(manual, nonce)
    assert submit(manual, nonce) == identifier
    assert await runtime.tasks.run(manual.step)
    result = manual.listing(service.session_id)['items'][0]
    assert result['status'] == 'completed' and result['result']['summary'] == '合成结果'
    assert calls == [None]
    assert service.store.state(PREFERENCES) == before
    assert not runtime.cores
    assert service.store.state('core-run:bilibili', {}).get('status') != 'active'
    assert service.store.state('core-run:manual')['status'] == 'paused'
    assert not await runtime.tasks.run(manual.step)


def test_unavailable_native_tasks_do_not_create_runs_or_reserve_budget(service):
    runtime, manual = prepare(service)
    for kind in ('web', 'learn', 'image'):
        with pytest.raises(AssistantError) as error:
            manual.submit(service.session_id, kind, {'prompt': 'no call'}, secrets.token_hex(16))
        assert error.value.code == 'model_capability_unavailable'
    assert not service.store.records('task_requests')[0]
    assert service.store.state('core-run:manual') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['cancel', 'stop_all', 'close', 'lock'])
async def test_inflight_cancel_drains_and_cannot_commit(service, boundary):
    runtime, manual = prepare(service)
    if boundary == 'lock':
        service.enable_lock('synthetic-password-123')
    entered, exited = asyncio.Event(), asyncio.Event()
    async def runner(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            exited.set()
    manual.runner = runner
    identifier = submit(manual)
    work = asyncio.create_task(runtime.tasks.run(manual.step))
    await asyncio.wait_for(entered.wait(), 2)
    if boundary == 'cancel':
        manual.control(service.session_id, identifier, 'cancel', manual.engine._state(identifier)['revision'])
    elif boundary == 'stop_all':
        runtime.stop_all(service.request())
    elif boundary == 'lock':
        service.lock()
    else:
        await asyncio.to_thread(runtime.close)
    await asyncio.wait_for(exited.wait(), 2)
    await asyncio.gather(work, return_exceptions=True)
    assert manual.engine._state(identifier)['status'] == 'cancelled'
    assert not service.store.records('task_results')[0]
    assert not runtime.tasks.attempts


@pytest.mark.asyncio
async def test_model_switch_repeats_only_uncommitted_stage_after_old_attempt_exit(service, tmp_path):
    service.bili = Bili()
    runtime, book, target = account_runtime(service, tmp_path, object())
    manual = runtime.manual
    entered, exited = asyncio.Event(), asyncio.Event()
    calls = []
    from qiban.desktop_siwc import KEY
    async def runner(task, previous, **kwargs):
        account = service.store.state(KEY)['active']
        calls.append((account, previous['step'] if previous else 0))
        if previous is None:
            return dict(phase='first', complete=False, payload={'saved': '保留'}, summary='')
        if account == book['active']:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                exited.set()
        assert exited.is_set()
        kwargs['guard']()
        return done()
    manual.runner = runner
    identifier = submit(manual)
    await runtime.tasks.run(manual.step)
    work = asyncio.create_task(runtime.tasks.run(manual.step))
    await asyncio.wait_for(entered.wait(), 2)
    service.codex.begin('select', account_id=target, expected=1)
    await asyncio.to_thread(service.codex.thread.join, 8)
    assert not service.codex.thread.is_alive()
    await asyncio.wait_for(work, 3)
    assert calls == [(book['active'], 0), (book['active'], 1), (target, 1)]
    assert manual.engine._state(identifier)['status'] == 'completed'
    assert len(service.store.records('task_results')[0]) == 2
    assert service.store.state('core-run:bilibili', {}).get('status') != 'active'


def test_restart_does_not_resubmit_and_old_session_cannot_read(service):
    runtime, manual = prepare(service)
    identifier = submit(manual)
    service.store.set_state('core-run:manual-video', {**service.store.state('core-run:manual'),
                                                   'accounts':[['bilibili','123']]})
    new = DesktopRuntime(service, 'manual-restarted', model=object())
    assert new.manual.engine._state(identifier)['status'] == 'cancelled'
    assert new.manual.engine._state(identifier)['error'] == 'application_restarted'
    with pytest.raises(AssistantError):
        manual.listing(service.session_id)
    assert not service.store.records('task_results')[0]
    assert service.store.state('core-run:manual-video')['status'] == 'paused'
    service.backups._paused()


@pytest.mark.asyncio
async def test_video_account_change_fails_without_read_and_retry_does_not_rebind(service):
    runtime, manual = prepare(service)
    identifier = submit(manual)
    service.bili.uid = '456'
    async def forbidden(*args, **kwargs):
        raise AssertionError('must not read another account')
    manual.runner = forbidden
    await runtime.tasks.run(manual.step)
    state = manual.engine._state(identifier)
    assert state['status'] == 'failed' and state['error'] == 'account_changed'
    with pytest.raises(AssistantError):
        manual.control(service.session_id, identifier, 'retry', state['revision'])


def test_route_validates_desktop_session_and_arguments(service):
    runtime, manual = prepare(service)
    assert not call(service, 'tasks.list', {'session': 'forged'})['ok']
    assert call(service, 'tasks.list', {'session': service.session_id})['ok']
    assert not call(service, 'tasks.submit', dict(session=service.session_id, kind=[], arguments={}, nonce='bad'))['ok']
    assert not service.store.records('task_requests')[0]


@pytest.mark.asyncio
async def test_restore_rejects_manual_work_and_allows_stopped_queue(service):
    runtime, manual = prepare(service)
    submit(manual)
    with pytest.raises(AssistantError) as error:
        service.backups._paused()
    assert error.value.code == 'restore_active'
    runtime.stop_all(service.request())
    service.backups._paused()


def test_retry_respects_queue_capacity_and_cancel_removes_pending_index(service):
    from qiban.desktop_manual_tasks import QUEUE
    runtime, manual = prepare(service)
    first = submit(manual)
    manual.control(service.session_id, first, 'cancel', manual.engine._state(first)['revision'])
    assert service.store.state(QUEUE) == []
    for _ in range(12):
        submit(manual)
    with pytest.raises(AssistantError) as error:
        manual.control(service.session_id, first, 'retry', manual.engine._state(first)['revision'])
    assert error.value.code == 'task_queue_full'
    assert len(service.store.state(QUEUE)) == 12


@pytest.mark.asyncio
async def test_long_unicode_results_are_complete_across_bounded_native_pages(service):
    import json
    runtime, manual = prepare(service)
    note = '中文🙂' * 5000
    async def runner(*args, **kwargs):
        return dict(phase='read', complete=True, payload={'notes':[note], 'intervals':[[0,60]]}, summary='已完成')
    manual.runner = runner
    identifier = submit(manual)
    await runtime.tasks.run(manual.step)
    pages, offset = [], 0
    while offset is not None:
        page = manual.detail(service.session_id, identifier, offset)
        assert len(json.dumps(page).encode()) < 262144
        pages.append(page['text'])
        offset = page['next']
    assert note in ''.join(pages)
    assert len(pages) > 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['web', 'learn'])
async def test_desktop_web_and_learning_reuse_full_private_workflows(service, tmp_path, kind):
    from companion_core.public_workspace import PublicWorkspace
    from tests_postgres.test_task_runner import NativeModel
    model = NativeModel(tmp_path)
    model.supported_native = {'web'}
    runtime = DesktopRuntime(service, 'manual-web-test', room_model=model)
    arguments = ({'objective':'learn conditions', 'criteria':['apply'], 'round_limit':2}
                 if kind == 'learn' else {'prompt':'查找公开材料'})
    identifier = runtime.manual.submit(service.session_id, kind, arguments, secrets.token_hex(16))['id']
    for _ in range(4):
        await runtime.tasks.run(runtime.manual.step)
    state = runtime.manual.engine._state(identifier)
    assert state['status'] == 'completed', state
    assert len(model.calls) == (4 if kind == 'learn' else 1)
    assert not runtime.cores and service.bili is None
    assert not PublicWorkspace(service.store).records('task_results')[0]
    assert not PublicWorkspace(service.store).records('memories')[0]
    page = runtime.manual.detail(service.session_id, identifier, 0)
    assert 'https://example.org/source' in page['text']


@pytest.mark.asyncio
async def test_desktop_web_rejects_missing_open_evidence_and_enforces_existing_quota(service, tmp_path):
    from tests_postgres.test_task_runner import NativeModel
    model = NativeModel(tmp_path)
    model.supported_native = {'web'}
    model.forged = True
    runtime = DesktopRuntime(service, 'manual-web-test', room_model=model)
    settings = service.store.read('companion')
    service.store.write('companion', {**settings['data'], 'native_web_daily_limit':1}, settings['version'], 'test')
    identifiers = []
    for _ in range(2):
        identifiers.append(runtime.manual.submit(service.session_id, 'web', {'prompt':'合成要求'}, secrets.token_hex(16))['id'])
        await runtime.tasks.run(runtime.manual.step)
    assert runtime.manual.engine._state(identifiers[0])['error'] == 'native_web_evidence'
    assert runtime.manual.engine._state(identifiers[1])['error'] == 'native_quota'
    assert len(model.calls) == 1 and not service.store.records('task_results')[0]


@pytest.mark.asyncio
async def test_restore_preserves_completed_history_and_retires_restored_queue(service):
    from companion_core import postgres_transfer
    from qiban.desktop_manual_tasks import HANDOFF, QUEUE
    runtime, manual = prepare(service)
    calls = []
    async def runner(task, *args, **kwargs):
        calls.append(task['id'])
        return done('备份中的结果🙂')
    manual.runner = runner
    completed = submit(manual)
    await runtime.tasks.run(manual.step)
    waiting = submit(manual)
    blob = postgres_transfer.create(service.store, service.store._backup_key)
    runtime.stop_all(service.request())
    later = submit(manual)
    runtime.stop_all(service.request())
    postgres_transfer.restore(service.store, blob, service.store._backup_key,
        expected=postgres_transfer.fingerprint(service.store), finalize=runtime.preserve_after_restore)
    assert manual.engine._state(waiting)['status'] == 'cancelled'
    assert manual.engine._state(waiting)['error'] == 'restore'
    assert service.store.state(QUEUE) == service.store.state(HANDOFF) == []
    assert not service.store.exists('task_requests', later)
    assert manual.detail(service.session_id, completed, 0)['text'] == '备份中的结果🙂'
    assert not await runtime.tasks.run(manual.step)
    assert calls == [completed]


@pytest.mark.asyncio
async def test_cancelling_last_queued_task_releases_run_without_marking_idle_worker_busy(service):
    runtime, manual = prepare(service)
    identifier = submit(manual)
    manual.control(service.session_id, identifier, 'cancel', manual.engine._state(identifier)['revision'])
    assert not await runtime.tasks.run(manual.step)
    assert not manual.engine.busy
    assert service.store.state('core-run:manual')['status'] == 'paused'
    service.backups._paused()


@pytest.mark.asyncio
async def test_historical_conflict_visible_without_rewriting_result_or_restarting(service):
    import json
    from copy import deepcopy

    runtime, manual = prepare(service)
    payload = {'passed': True, 'gaps': [], 'round': 1,
               'grades': [{'id': str(i), 'passed': True, 'reason': '原始理由🙂' * 200,
                           'gap': '需要核对材料' if i == 0 else ''} for i in range(8)],
               'limitations': '仅为模型评阅，未独立实测。',
               'sources': [{'title': '来源', 'url': 'https://example.org/source', 'note': '材料摘要'}]}
    async def runner(*args, **kwargs):
        return dict(phase='assessed', complete=True, payload=deepcopy(payload), summary='模型自检通过；旧汇总。')
    manual.runner = runner
    identifier = submit(manual)
    await runtime.tasks.run(manual.step)
    result_id = manual.engine._state(identifier)['latest']
    original = deepcopy(service.store.read('task_results', result_id))
    summary = manual.listing(service.session_id)['items'][0]['result']['summary']
    assert '矛盾' in summary and '未通过' in summary and '自检通过' not in summary
    pages, offset = [], 0
    while offset is not None:
        page = manual.detail(service.session_id, identifier, offset)
        assert len(json.dumps(page).encode()) < 262144
        pages.append(page['text'])
        offset = page['next']
    text = ''.join(pages)
    assert len(pages) > 1
    for grade in payload['grades']:
        assert grade['reason'] in text and grade['gap'] in text
    assert '模型原始判定：通过' in text and '本题不计通过' in text
    assert payload['limitations'] in text and 'https://example.org/source' in text
    assert service.store.read('task_results', result_id) == original
    assert manual.engine._state(identifier)['status'] == 'completed'
    assert not await runtime.tasks.run(manual.step)
