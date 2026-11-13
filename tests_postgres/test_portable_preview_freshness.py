"""A portable preview must survive the worker's own idle maintenance, and nothing else.

`DesktopRuntime.maintain()` runs on the platform scheduler thread of the desktop worker
immediately at startup and every 60 s afterwards, also while every platform is stopped.
`DesktopPortable.preview()` captures `postgres_transfer.fingerprint` and `complete()`
compares it again, so any write a maintenance pass makes in between rejects the switch
with `portable_changed / 当前资料在预览后已变化，请重新检查`, without the user changing
anything. These cases pin the invariant (a pass with nothing to do writes nothing), the
user visible consequence, the bookkeeping of a pass that really removed something, and the
guard that must stay (a real edit still refuses).
"""
from datetime import datetime, timedelta, timezone

import pytest

from companion_core import events, postgres_transfer
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.fact_retirement import cleanup as fact_cleanup
from companion_core.governance import GovernedContext
from companion_core.media_originals import MediaOriginals
from qiban.desktop_portable import DesktopPortable
from qiban.desktop_profile import desktop_profile
from qiban.desktop_tasks import TaskBarrier
from tests_postgres.test_desktop_management import desktop_store, service
from tests_postgres.test_fact_retirement import stop as retire_facts
from tests_postgres.test_personal_fact_management import seed_chain

__all__ = ['desktop_store', 'service']

PASSWORD = 'portable-freshness-password'
SUCCESS = {'switched': True, 'restart_required': True, 'restored': True}


@pytest.fixture
def pg_profile():
    """The desktop profile the managed application itself uses."""
    return desktop_profile()


class Stager:
    """Records the hand-off; the switch decision itself is not part of these cases."""

    def __init__(self):
        self.calls = []

    def __call__(self, **arguments):
        self.calls.append(arguments)
        return dict(SUCCESS)


def portable(service, stager):
    return DesktopPortable(service, key_provider=lambda: service.store._test_key, stager=stager)


def exported(portable_service, directory):
    target = directory / 'freshness.ccbackup'
    result = portable_service.export(str(target), PASSWORD)
    return target, directory / result['key_file']


def snapshot(store):
    with store.db() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return postgres_transfer._snapshot(store, db)


def changed_rows(before, after):
    """Which rows of the fingerprinted snapshot differ, named by table (and state key)."""
    changes = []
    for table in before['tables']:
        columns, old_rows, new_rows = (before['tables'][table]['columns'],
                                       before['tables'][table]['rows'], after['tables'][table]['rows'])
        if table == 'state':
            index = columns.index('key')
            old = {row[index]: row for row in old_rows}
            new = {row[index]: row for row in new_rows}
            changes.extend('state:' + key for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key))
        elif {repr(row) for row in old_rows} != {repr(row) for row in new_rows}:
            changes.append(table)
    return changes


def maintenance_pass(store):
    """Exactly what DesktopRuntime.maintain() runs while no manual task is busy."""
    MediaOriginals(store).cleanup(guard=lambda: None)
    with TaskBarrier().idle_maintenance() as idle:
        assert idle
        fact_cleanup(store, guard=lambda: None, reclaim_snapshots=True)


def steady(store):
    """Build the retention index and let its one-off records settle: both are real changes."""
    for _ in range(10):
        maintenance_pass(store)
        if store.state('source-retention-index-v1', {}).get('stage') == 'ready':
            break
    assert store.state('source-retention-index-v1', {}).get('stage') == 'ready'
    maintenance_pass(store)


def retirement_markers(store):
    with store.db() as db:
        return db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'fact-retired:%'").fetchone()[0]


def seed_independent_facts(store, count=2):
    """One personal fact per identity, seeded the way the repository's own fixtures do.

    `Event`'s fifth field is the conversation and it forms the fact scope together with the
    attribute, so a distinct conversation per record keeps the retirement plans apart and each
    fact can be retired and cleaned in its own batch.
    """
    identifiers = []
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with store.lock, store.db():
        for index in range(count):
            event = Event('alpha', 'bot', f'freshness-{index}', ActorRef('alpha', 'a'),
                          f'dm-freshness-{index}', (start + timedelta(hours=index)).isoformat(),
                          f'我住城市{index}')
            events.ingest(store, event)
            governed = GovernedContext(store, event)
            governed.classify_event()
            identifier = f'fact-freshness-{index:04}'
            store.write('memories', {'schema_version': 1, 'id': identifier, 'kind': 'user_preference',
                                     'content': event.text, 'confidence': 'explicit',
                                     'tags': ['personal_fact'], 'locked': False,
                                     'source': {'type': 'interaction', 'recorded_at': event.occurred_at,
                                                'reference': event.id},
                                     'fact': {'actor': event.actor.key, 'account': event.account,
                                              'conversation': event.thread, 'attribute': 'residence',
                                              'value': f'城市{index}', 'quote': event.text,
                                              'valid_from': event.occurred_at, 'supersedes': []}},
                        None, 'synthetic', identifier)
            governed.classify_written('memories', identifier, personal=True, sources=['event/' + event.id])
            identifiers.append(identifier)
    return identifiers


def test_idle_maintenance_pass_writes_nothing(desktop_store):
    steady(desktop_store)
    before, fingerprint = snapshot(desktop_store), postgres_transfer.fingerprint(desktop_store)
    for _ in range(3):
        maintenance_pass(desktop_store)
    assert changed_rows(before, snapshot(desktop_store)) == [], '没有待处理内容的维护轮次改写了资料库'
    assert postgres_transfer.fingerprint(desktop_store) == fingerprint


def test_preview_survives_an_idle_maintenance_pass(service, tmp_path):
    stager = Stager()
    portable_service = portable(service, stager)
    steady(service.store)
    backup, key_file = exported(portable_service, tmp_path)
    preview = portable_service.preview(str(backup), str(key_file), PASSWORD)
    maintenance_pass(service.store)
    challenge = portable_service.begin(preview['token'])['challenge']
    result = portable_service.complete(challenge)
    assert result['switched'] is True and result['restart_required'] is True
    assert stager.calls, '维护轮次之后，预览仍然必须能够完成切换'


def test_real_edit_between_preview_and_complete_is_still_refused(service, tmp_path):
    stager = Stager()
    portable_service = portable(service, stager)
    backup, key_file = exported(portable_service, tmp_path)
    preview = portable_service.preview(str(backup), str(key_file), PASSWORD)
    identifier = 'freshness-memory'
    written = service.dispatch({'action': 'management', 'operation': 'records.correct',
                                'request_id': 'f' * 32,
                                'arguments': {'kind': 'memories', 'identifier': identifier, 'expected': None,
                                              'value': {'schema_version': 1, 'id': identifier,
                                                        'kind': 'user_preference', 'content': '合成资料',
                                                        'confidence': 'observed', 'tags': [], 'locked': False,
                                                        'source': {'type': 'user_instruction',
                                                                   'reference': 'freshness',
                                                                   'recorded_at': '2026-11-07T00:00:00Z'}}}})
    assert written['ok'], written
    challenge = portable_service.begin(preview['token'])['challenge']
    with pytest.raises(AssistantError) as refused:
        portable_service.complete(challenge)
    assert refused.value.code == 'portable_changed'
    assert '当前资料在预览后已变化' in str(refused.value)
    assert not stager.calls


def test_real_cleanup_removal_between_preview_and_complete_is_still_refused(service, tmp_path):
    """The rejection must survive for real maintenance: only no-op bookkeeping became silent."""
    stager = Stager()
    portable_service = portable(service, stager)
    steady(service.store)
    # The repository's own retirement helpers seed one memory with facts and retire it, which
    # is what writes the `fact-retired:` marker this maintenance pass deletes.
    identifier = seed_chain(service.store, 1)
    retired, _ = retire_facts(service, identifier)
    assert retired['ok'] and retired['result']['count'] == 1, retired
    backup, key_file = exported(portable_service, tmp_path)
    preview = portable_service.preview(str(backup), str(key_file), PASSWORD)
    removed = fact_cleanup(service.store, clock=lambda: datetime.now(timezone.utc) + timedelta(days=91),
                           guard=lambda: None)
    assert removed['removed'] == 1, removed
    assert service.store.state('fact-cleanup-last')['removed'] == 1, '真实删除仍必须留下清理记录'
    challenge = portable_service.begin(preview['token'])['challenge']
    with pytest.raises(AssistantError) as refused:
        portable_service.complete(challenge)
    assert refused.value.code == 'portable_changed'
    assert not stager.calls


def test_each_real_batch_records_its_own_time_even_with_equal_counts(service):
    """Two batches that each remove one fact share counts; each one still stamps its own time.

    `facts.retention` renders `最近一批检查 …` from `fact-cleanup-last`, so a pass that really
    removed something must record *its* clock, not keep the previous batch's stamp.
    """
    steady(service.store)
    identifiers = seed_independent_facts(service.store, 2)
    for identifier in identifiers:
        retired, _ = retire_facts(service, identifier)
        assert retired['ok'] and retired['result']['count'] == 1, retired
    assert retirement_markers(service.store) == 2

    def stored(identifier):
        with service.store.db() as db:
            return db.execute("SELECT COUNT(*) FROM content_records WHERE kind='memories' AND id=?",
                              (identifier,)).fetchone()[0] == 1

    assert stored(identifiers[0]) and stored(identifiers[1])
    now = [datetime.now(timezone.utc) + timedelta(days=91)]

    def clock():
        return now[0]

    first = fact_cleanup(service.store, clock=clock, guard=lambda: None, limit=1)
    assert first['removed'] == 1 and not stored(identifiers[0]), first
    assert service.store.state('fact-cleanup-last')['at'] == now[0].isoformat()
    now[0] += timedelta(days=1)
    second = fact_cleanup(service.store, clock=clock, guard=lambda: None, limit=1)
    assert second['removed'] == 1 and not stored(identifiers[1]), second
    assert first == second, '构造失败：两批的汇总结果必须相同'
    assert service.store.state('fact-cleanup-last')['at'] == now[0].isoformat()
