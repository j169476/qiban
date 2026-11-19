"""D48: stale automatic snapshots only; actual evidence and tasks retain sources."""
import base64
import json
import subprocess
import sys

import pytest

from companion_core import fact_retirement, postgres_transfer, source_retention
from companion_core.access import RecordPolicy
from companion_core.errors import AssistantError
from tests_postgres.test_desktop_management import desktop_store, service
from tests_postgres.test_fact_retirement import later, stop
from tests_postgres.test_fact_trace_governance import facts, seed
from tests_postgres.test_personal_fact_management import seed_chain

__all__ = ['desktop_store', 'service']


def prepared(service, *, retired=True):
    seed(service.store)
    _, governed, value = facts(service.store, 'snapshot-read', '住所')
    identifier = value.context()['items'][0]['id']
    root = next(r for r in governed.resources if r.startswith(source_retention.VIEW))
    if retired:
        assert stop(service, identifier)[0]['ok']
    return identifier, root


def clean(service, **kwargs):
    return fact_retirement.cleanup(service.store, clock=later, reclaim_snapshots=True, **kwargs)


def test_unused_snapshot_reclaimed_but_body_obeys_retention_and_watermark(service):
    identifier, root = prepared(service)
    result = fact_retirement.cleanup(service.store, reclaim_snapshots=True)
    assert result['snapshots']['removed'] == 1 and result['removed'] == 0
    assert service.store.exists('memories', identifier) and service.repository.policy(root) is None
    assert clean(service)['removed'] == 1
    assert not service.store.exists('memories', identifier)
    assert not service.repository.policy('memories/' + identifier).valid
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'fact-retired-scope:%'").fetchone()[0] == 1
    assert clean(service)['removed'] == 0


def test_valid_unretired_history_and_its_snapshot_are_never_collected(service):
    identifier, root = prepared(service, retired=False)
    before = service.store.read('memories', identifier)
    assert clean(service)['snapshots']['removed'] == 0
    assert service.repository.policy(root) and service.store.read('memories', identifier) == before


def test_time_budget_resumes_at_last_examined_node_without_skipping(service, monkeypatch):
    from types import SimpleNamespace

    from companion_core import fact_projection, fact_snapshot_cleanup

    identifier, root = prepared(service, retired=False)
    _, governed, _ = facts(service.store, 'more-snapshots', '住所')
    original = service.repository.policy(root)
    with service.store.db() as db:
        for signature in ('synthetic-two', 'synthetic-three'):
            fact_projection.persist(service.repository, db, governed.event, signature, list(original.source_versions))
    assert stop(service, identifier)[0]['ok']
    clock = [0]
    monkeypatch.setattr(fact_snapshot_cleanup, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def slow(*args, **kwargs):
        clock[0] += 3
        return True
    monkeypatch.setattr(fact_snapshot_cleanup, 'stale', slow)
    results = [clean(service) for _ in range(3)]
    assert [r['snapshots']['examined'] for r in results] == [1, 1, 1]
    assert sum(r['snapshots']['removed'] for r in results) == 3
    assert results[-1]['removed'] == 1


def test_incomplete_dependency_index_is_reported_and_never_mistaken_for_deleted_source(service):
    identifier, root = prepared(service, retired=False)
    with service.store.db() as db:
        db.execute('DELETE FROM source_dependencies WHERE derived=?', (root,))
    result = clean(service)
    assert result['snapshots']['unverified'] == 1 and result['snapshots']['removed'] == 0
    assert service.repository.policy(root) and service.store.exists('memories', identifier)


@pytest.mark.asyncio
async def test_real_reply_pipeline_indexes_action_and_trace_roots(service):
    from companion_core.runtime import Companion
    from tests_postgres.test_runtime import Adapter, Host, Model

    seed(service.store)
    core = Companion(service.store, Model(), Host(), [Adapter()])
    core.start()
    event, _, _ = facts(service.store, 'retention-reply', '住所')
    await core.handle(event)
    with service.store.db() as db:
        rows = db.execute('SELECT owner_kind,resource FROM source_retention WHERE resource LIKE ?',
                          (source_retention.VIEW + '%',)).fetchall()
    assert {r['owner_kind'] for r in rows} == {'state', 'core_action'}


def test_upgrade_interruption_rolls_back_schema_and_reopen_preserves_records(service, monkeypatch):
    from companion_core.pg_connection import Connection

    identifier, _ = prepared(service, retired=False)
    before = service.store.read('memories', identifier)
    with service.store.db() as db:
        db.execute('DROP FUNCTION remove_source_retention() CASCADE')
        db.execute('DROP TABLE source_retention')
        db.execute('DROP INDEX governance_audit_resource')
        db.execute('DELETE FROM schema_migrations WHERE version=15')
    original = Connection.executescript
    def interrupted(connection, sql):
        result = original(connection, sql)
        if 'CREATE TABLE source_retention' in sql:
            raise RuntimeError('synthetic migration interruption')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(Connection, 'executescript', interrupted)
        with pytest.raises(RuntimeError, match='synthetic'):
            service.store.migrate()
    with service.store.db() as db:
        assert db.execute("SELECT to_regclass('source_retention')").fetchone()[0] is None
        assert db.execute('SELECT MAX(version) FROM schema_migrations WHERE version<=15').fetchone()[0] == 14
    service.store.migrate()
    service.store.migrate()
    assert service.store.read('memories', identifier) == before
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=15').fetchone()[0] == 1


@pytest.mark.parametrize('owner', ['task', 'reply_trace', 'bili_action', 'sent', 'uncertain'])
def test_actual_references_retain_invalid_snapshot_and_body_until_owner_removed(service, owner):
    identifier, root = prepared(service)
    version = service.repository.policy(root).version
    with service.store.db() as db:
        if owner == 'task':
            service.repository.set_policy(RecordPolicy('task_results/synthetic', 'v1', 'personal',
                sources=(root,), source_versions=((root, version),)), None, 'test', db=db)
        elif owner == 'reply_trace':
            service.store.set_state('reply-trace:synthetic', {'sources': [], 'retention_roots': [root]}, db)
        elif owner == 'bili_action':
            service.store.set_state('action-provenance:synthetic', {'pins': [[root, version]]}, db)
        else:
            body = {'disclosure': {'pins': [[root, version]]}}
            db.execute('INSERT INTO core_actions VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('synthetic', 'synthetic', 'g', 'qq', 'synthetic', 'send',
                 service.store.ledger_encode('core_actions.body', body), owner, None, '2026-01-01'))
            source_retention.action_refs(db, 'synthetic', body)
    assert clean(service)['snapshots']['removed'] == 0
    assert service.store.exists('memories', identifier)
    with service.store.db() as db:
        if owner == 'task':
            db.execute("DELETE FROM governance_policies WHERE resource='task_results/synthetic'")
        elif owner in ('reply_trace', 'bili_action'):
            key = 'reply-trace:synthetic' if owner == 'reply_trace' else 'action-provenance:synthetic'
            db.execute('DELETE FROM state WHERE key=?', (key,))
        else:
            db.execute("DELETE FROM core_actions WHERE id='synthetic'")
    assert clean(service)['removed'] == 1
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM source_retention').fetchone()[0] == 0


def test_incomplete_old_trace_blocks_collection_without_exposing_internal_roots(service):
    from companion_core.reply_trace import listing
    identifier, root = prepared(service)
    service.store.set_state('reply-trace:old', {'id': 'old', 'sources': [], 'sources_limited': True})
    assert clean(service)['snapshots']['removed'] == 0
    service.store.set_state('reply-trace:old', {'id': 'old', 'sources': [], 'sources_limited': True,
                                              'retention_roots': [root]})
    assert 'retention_roots' not in listing(service.store)[0]
    assert clean(service)['removed'] == 0
    with service.store.db() as db:
        db.execute("DELETE FROM state WHERE key='reply-trace:old'")
    assert clean(service)['removed'] == 1


def test_legacy_reference_indexing_is_bounded_resumable_and_blocks_early_collection(service):
    identifier, root = prepared(service)
    version = service.repository.policy(root).version
    with service.store.db() as db:
        # Raw inserts reproduce pre-upgrade encrypted ledgers without the new index.
        for i in range(102):
            key = f'action-provenance:{i:04}'
            db.execute('INSERT INTO state VALUES(?,?)', (key, service.store.ledger_encode('state:' + key,
                {'pins': [[root, version]]} if i == 101 else {'pins': []})))
    first = clean(service)
    assert first['snapshots']['status'] == 'indexing' and first['removed'] == 0
    assert service.repository.policy(root)
    second = clean(service)
    assert second['snapshots']['status'] == 'checked' and second['removed'] == 0
    with service.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM source_retention WHERE resource=?', (root,)).fetchone()[0] == 1


def test_raw_fact_trace_reference_protects_body_even_after_snapshot_collection(service):
    identifier, root = prepared(service)
    service.store.set_state('reply-trace:direct', {'sources': [{'resource': 'memories/' + identifier, 'version': 'v'}]})
    result = clean(service)
    assert result['snapshots']['removed'] == 1 and result['removed'] == 0
    assert service.repository.policy(root) is None and service.store.exists('memories', identifier)


def test_shared_descendants_of_retained_root_survive_other_root_collection(service):
    seed_chain(service.store, 40)
    _, first, value = facts(service.store, 'first-view', '住所')
    root = next(r for r in first.resources if r.startswith(source_retention.VIEW))
    # A later independent fact reuses branches without depending on the old root.
    newer = facts(service.store, 'new-view', '我住新城')[2]
    assert newer.save([{'attribute': 'residence', 'value': '新城', 'quote': '我住新城',
                       'valid_from': None, 'supersedes': []}])['written'] == 1
    _, latest, _ = facts(service.store, 'last-view', '住所')
    last = next(r for r in latest.resources if r.startswith(source_retention.VIEW))
    old_sources = set(service.repository.policy(root).sources)
    shared = old_sources & set(service.repository.policy(last).sources)
    assert any(r.startswith(source_retention.VIEW) for r in shared)
    assert stop(service, value.headers[0]['id'])[0]['ok']
    service.store.set_state('reply-trace:keep', {'sources': [], 'retention_roots': [last]})
    for _ in range(4):
        clean(service, limit=10)
    assert service.repository.policy(root) is None and service.repository.policy(last)
    assert all(service.repository.policy(r) for r in shared)


def test_collection_and_index_backfill_roll_back_on_interruption_and_restore_keeps_holds(service):
    identifier, root = prepared(service)
    def interrupted():
        if service.repository.policy(root) is None:
            raise AssistantError('stale_runtime', 'synthetic interruption after snapshot delete')
    with pytest.raises(AssistantError, match='synthetic'):
        clean(service, guard=interrupted)
    assert service.repository.policy(root) and service.store.exists('memories', identifier)
    assert service.store.state(source_retention.INDEX) is None
    service.store.set_state('reply-trace:kept', {'sources': [], 'retention_roots': [root]})
    blob = postgres_transfer.create(service.store, service.store._backup_key)
    with service.store.db() as db:
        db.execute("DELETE FROM state WHERE key='reply-trace:kept'")
    assert clean(service)['removed'] == 1
    postgres_transfer.restore(service.store, blob, service.store._backup_key,
                              expected=postgres_transfer.fingerprint(service.store))
    assert clean(service)['removed'] == 0
    assert service.repository.policy(root) and service.store.exists('memories', identifier)


@pytest.mark.parametrize('stage', ['before_commit', 'after_commit'])
def test_process_exit_has_atomic_snapshot_body_index_and_cursor_outcome(service, stage):
    identifier, root = prepared(service)
    script = '''
import base64,json,os,sys
from datetime import datetime,timedelta,timezone
from companion_core.postgres import PostgresStore
from companion_core.fact_retirement import cleanup
from companion_core.governance import GovernanceRepository
v=json.load(sys.stdin)
s=PostgresStore(v['root'],v['options'],base64.b64decode(v['key']))
def guard():
 if v['stage']=='before_commit' and GovernanceRepository(s).policy(v['view']) is None:os._exit(96)
r=cleanup(s,guard=guard,reclaim_snapshots=True,clock=lambda:datetime.now(timezone.utc)+timedelta(days=91))
assert r['removed']==1
os._exit(96)
'''
    payload = {'root': str(service.store.root), 'options': service.store.database._options,
               'key': base64.b64encode(service.store._test_key).decode(), 'view': root, 'stage': stage}
    child = subprocess.run([sys.executable, '-c', script], input=json.dumps(payload), text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.returncode == 96
    exists = stage == 'before_commit'
    assert bool(service.repository.policy(root)) == exists
    assert service.store.exists('memories', identifier) == exists
    assert clean(service)['removed'] == int(exists)
