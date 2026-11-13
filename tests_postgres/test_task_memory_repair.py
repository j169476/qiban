"""Generated-memory correction uses real isolated PostgreSQL; no model calls."""
import secrets
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core import postgres_transfer
from companion_core import task_memory_repair as repair
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.governance import ReadView
from companion_core.record_maintenance import remove
from qiban.desktop_runtime import DesktopRuntime
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as management_fixture

service = management_fixture


def runtime_for(service, name='repair-fixture'):
    return DesktopRuntime(service, name, room_model=SimpleNamespace(supported_native={'web'}))


async def seed(runtime, *, conflict=True):
    """Simulate the old app's completed learning task and generated memory."""
    manual = runtime.manual
    async def runner(*args, **kwargs):
        return {'phase': 'assessed', 'complete': True, 'summary': '模型自检通过；旧摘要。',
                'payload': {'passed': True, 'gaps': [], 'round': 1,
                            'grades': [{'id': 'q1', 'passed': True, 'reason': '原始理由',
                                        'gap': '待核实边界' if conflict else ''}],
                            'limitations': '模型自检，未独立验证。'}}
    manual.runner = runner
    identifier = manual.submit(runtime.management.session_id, 'learn',
        {'objective': '合成任务', 'criteria': ['边界'], 'round_limit': 1}, secrets.token_hex(16))['id']
    assert await runtime.tasks.run(manual.step)
    return identifier, stable_key('task-memory', identifier)


def reset_scan(store):
    store.set_state(repair.STATE, {'after': '', 'scanned': 0, 'corrected': 0, 'complete': False})


def history(store, identifier):
    with store.db() as db:
        return list(db.execute("SELECT version,body FROM content_history WHERE kind='memories' AND id=?",
                               (identifier,)))


@pytest.mark.asyncio
async def test_correction_preserves_history_metadata_privacy_and_invalidates_old_lease(service):
    runtime = runtime_for(service)
    task, memory = await seed(runtime)
    store = service.store
    old = store.read('memories', memory)
    old = store.write('memories', {**old['data'], 'tags': ['explicit_task', '保留标签'], 'locked': True},
                      old['version'], 'fixture-admin', memory)
    result_id = runtime.manual.engine._state(task)['latest']
    result = deepcopy(store.read('task_results', result_id))
    before_policy = service.repository.policy('memories/' + memory)
    request = runtime.manual.request(service.session_id)
    lease = runtime.manual.engine.access.lease(request, ['memories/' + memory])
    reset_scan(store)
    report = repair.repair(store)
    assert report['complete'] and report['corrected'] == 1
    current = store.read('memories', memory)
    assert current['version'] != old['version']
    assert '更正：' in current['data']['content'] and '未通过' in current['data']['content']
    assert '待核实边界' in current['data']['content'] and '未独立验证' in current['data']['content']
    assert {**current['data'], 'content': old['data']['content']} == old['data']
    assert replace(service.repository.policy('memories/' + memory), version=before_policy.version) == before_policy
    archived = next(row for row in history(store, memory) if row['version'] == old['version'])
    assert store._decode('memories', memory, archived['version'], archived['body']) == old
    assert store.read('task_results', result_id) == result
    with pytest.raises(AssistantError):
        runtime.manual.engine.access.revalidate(lease)
    readable = service.repository.readable_resources(runtime.manual.engine.access, request)
    assert ReadView(store, readable).read('memories', memory) == current
    assert before_policy.audience == 'private'
    assert repair.repair(store) == report
    assert store.read('memories', memory) == current
    assert len([row for row in history(store, memory) if row['version'] == old['version']]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['edited', 'deleted', 'not_conflicting', 'new_summary'])
async def test_repair_does_not_overwrite_other_content_or_resurrect(service, case):
    runtime = runtime_for(service)
    task, memory = await seed(runtime, conflict=case != 'not_conflicting')
    store = service.store
    old = store.read('memories', memory)
    if case == 'edited':
        old = store.write('memories', {**old['data'], 'content': '管理员自己的更正'}, old['version'], 'admin', memory)
    if case == 'deleted':
        remove(store, 'memories', memory, old['version'], 'admin')
    if case == 'new_summary':
        # New code already generated its effective nonpass summary.
        result_id = runtime.manual.engine._state(task)['latest']
        result = store.read('task_results', result_id)
        summary = repair.assessment(result['data']['payload']['grades'])['summary']
        store.write('task_results', {**result['data'], 'summary': summary}, result['version'], 'fixture', result_id)
        old = store.write('memories', {**old['data'], 'content': repair.PREFIX + summary}, old['version'], 'fixture', memory)
    before_history = len(history(store, memory))
    reset_scan(store)
    assert repair.repair(store)['corrected'] == 0
    assert len(history(store, memory)) == before_history
    if case == 'deleted':
        assert not store.exists('memories', memory)
    else:
        assert store.read('memories', memory) == old


@pytest.mark.asyncio
async def test_interrupted_page_rolls_back_memory_history_and_cursor(service, monkeypatch):
    runtime = runtime_for(service)
    identifiers = [(await seed(runtime))[1] for _ in range(2)]
    store = service.store
    before = {key: store.read('memories', key) for key in identifiers}
    reset_scan(store)
    cursor = store.state(repair.STATE)
    write = store.write
    count = 0
    def failing(*args, **kwargs):
        nonlocal count
        result = write(*args, **kwargs)
        count += 1
        if count == 2:
            raise RuntimeError('simulated interruption after second write')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(store, 'write', failing)
        with pytest.raises(RuntimeError):
            repair.repair_batch(store)
    assert store.state(repair.STATE) == cursor
    for key in identifiers:
        assert store.read('memories', key) == before[key]
        assert history(store, key) == []
    assert repair.repair(store)['corrected'] == 2


@pytest.mark.asyncio
async def test_startup_resumes_committed_batch_without_duplicate_versions(service, monkeypatch):
    runtime = runtime_for(service)
    identifiers = [(await seed(runtime))[1] for _ in range(2)]
    reset_scan(service.store)
    monkeypatch.setattr(repair, 'BATCH', 1)
    first = repair.repair_batch(service.store)
    assert first['corrected'] == 1 and not first['complete']
    runtime_for(service, 'restarted')
    assert service.store.state(repair.STATE)['corrected'] == 2
    assert all(len(history(service.store, key)) == 1 for key in identifiers)
    runtime_for(service, 'restarted-again')
    assert all(len(history(service.store, key)) == 1 for key in identifiers)


@pytest.mark.asyncio
async def test_restoring_older_backup_rechecks_before_release(service):
    runtime = runtime_for(service)
    _, memory = await seed(runtime)
    store = service.store
    old = store.read('memories', memory)
    # This intentionally includes a completed marker: restore must reset it.
    blob = postgres_transfer.create(store, store._backup_key)
    reset_scan(store)
    repair.repair(store)
    corrected = store.read('memories', memory)
    postgres_transfer.restore(store, blob, store._backup_key,
        expected=postgres_transfer.fingerprint(store), finalize=runtime.preserve_after_restore)
    assert store.read('memories', memory) == corrected
    rows = history(store, memory)
    assert len(rows) == 1 and rows[0]['version'] == old['version']
    assert store.state(repair.STATE)['corrected'] == 1
    assert store.state('desktop-manual-queue') == []
