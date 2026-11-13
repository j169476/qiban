import secrets
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from companion_core import events, postgres_transfer
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.media_originals import MIB, MediaOriginals
from companion_core.postgres import PostgresStore
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


@pytest.fixture
def library(desktop_store):
    media = MediaOriginals(desktop_store)
    scope = ReplyScope('qq', '10001', 'group', '10003')
    grant = ReplyAuthorizations(desktop_store).update(scope, enabled=True, expected_version=None, actor='test')['version']
    media.configure_capacity(1, None, actor='test')

    def source(number, size=600*1024):
        identifier = stable_key('qq-native-attachment', 'capacity', number)
        event = Event('qq', '10001', 'capacity-' + str(number), ActorRef('qq', '10002', '10001'),
                      'group:10003', '2026-10-31T00:00:00+00:00', '合成附件', 'group',
                      attachments=({'id': identifier, 'kind': 'file', 'position': 0},))
        version = events.ingest(desktop_store, event)['version']
        return (scope, grant, event.id, version, identifier, bytes([number % 256])*size)

    return media, source


def put(media, args, guard=lambda: None):
    return media.put(*args, name='合成测试.txt', guard=guard)


def finish(media, args):
    media.store.set_state('core-processed:' + args[2], args[3])


def test_admission_reclaims_oldest_processed_before_thirty_days(library):
    media, source = library
    old, new = source(1), source(2)
    put(media, old)
    finish(media, old)
    put(media, new)
    rows = {row['id']: row for row in media.listing()['items']}
    assert rows[old[4]]['status'] == 'expired' and rows[old[4]]['cleanup_reason'] == 'capacity'
    assert rows[new[4]]['status'] == 'available'
    assert events.get(media.store, old[2])['event']['text'] == '合成附件'
    assert media.store.state('core-processed:' + old[2]) == old[3]
    assert media.capacity()['used_bytes'] == 600*1024
    with pytest.raises(AssistantError):
        put(media, old)  # No silent resurrection of reclaimed originals.


@pytest.mark.parametrize('protection', ['pending', 'hold', 'saved', 'favorite', 'emoticon'])
def test_capacity_never_deletes_protected_originals(library, protection):
    media, source = library
    old, new = source(1), source(2)
    first = put(media, old)
    if protection != 'pending':
        finish(media, old)
    if protection == 'hold':
        media.hold('owner', 'task', [old[4]])
    elif protection in {'saved', 'favorite', 'emoticon'}:
        media.preserve(old[4], [protection], first['revision'], actor='test')
    with pytest.raises(AssistantError) as error:
        put(media, new)
    assert error.value.code == 'media_capacity_full'
    assert media.read(old[0], old[1], old[4], guard=lambda: None) == old[5]
    assert media.capacity()['protected_bytes'] == len(old[5])
    assert len(media.listing()['items']) == 1


def test_insufficient_reclaimable_space_rolls_back_partial_eviction(library):
    media, source = library
    old, protected, large = source(1, 100*1024), source(2, 800*1024), source(3, 300*1024)
    put(media, old)
    finish(media, old)
    put(media, protected)
    with pytest.raises(AssistantError) as error:
        put(media, large)
    assert error.value.code == 'media_capacity_full'
    assert all(row['status'] == 'available' for row in media.listing()['items'])
    assert media.capacity()['used_bytes'] == 900*1024


def test_cancel_after_reclamation_rolls_back_both_delete_and_insert(library):
    media, source = library
    old, new = source(1), source(2)
    put(media, old)
    finish(media, old)
    calls = []
    def cancel():
        calls.append(1)
        if len(calls) == 3:
            raise AssistantError('cancelled', 'synthetic cancellation before commit')
    with pytest.raises(AssistantError) as error:
        put(media, new, cancel)
    assert error.value.code == 'cancelled'
    assert [r['id'] for r in media.listing()['items']] == [old[4]]
    assert media.listing()['items'][0]['status'] == 'available'
    put(media, new)  # Explicit next test attempt, not application auto-retry.
    assert media.capacity()['used_bytes'] == len(new[5])


def test_real_connection_loss_after_eviction_restores_original_and_releases_write_lock(library):
    media, source = library
    old, new = source(1), source(2)
    put(media, old)
    finish(media, old)
    calls = []
    def interrupt():
        calls.append(1)
        if len(calls) == 3:
            with media.store.db() as db:
                pid = db.execute('SELECT pg_backend_pid()').fetchone()[0]
            # Kill only this disposable test transaction's identified backend.
            with psycopg.connect(**media.store.database._options, autocommit=True) as control:
                assert control.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0]
    with pytest.raises(AssistantError) as error:
        put(media, new, interrupt)
    assert error.value.code == 'database_error'
    assert media.read(old[0], old[1], old[4], guard=lambda: None) == old[5]
    put(media, new)
    assert media.capacity()['used_bytes'] <= MIB


def test_independent_writers_cannot_overfill_pending_storage(library, tmp_path):
    media, source = library
    args = [source(1), source(2)]
    # Separate Store locks/pools simulate independently owned writers. The
    # shared database transaction lock, not a Python lock, serializes admission.
    second = PostgresStore(tmp_path/'other-writer', media.store.database._options, media.store._test_key)
    def attempt(pair):
        current, values = pair
        try:
            put(current, values)
            return 'stored'
        except AssistantError as error:
            return error.code
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, [(media, args[0]), (MediaOriginals(second), args[1])]))
        assert sorted(outcomes) == ['media_capacity_full', 'stored']
        assert media.capacity()['used_bytes'] == 600*1024
    finally:
        second.database.close()


def test_lowered_capacity_is_bounded_and_preserves_holds_across_reopen(library):
    media, source = library
    old = media.capacity_settings()
    media.configure_capacity(3, old['revision'], actor='test')
    values = [source(n) for n in range(1, 5)]
    for args in values:
        put(media, args)
        finish(media, args)
    media.hold('owner', 'task', [values[0][4]])
    old = media.capacity_settings()
    media.configure_capacity(1, old['revision'], actor='test')
    media = MediaOriginals(media.store)
    assert media.cleanup(limit=1)['expired'] == 1
    assert media.capacity()['over_limit']
    assert media.cleanup(limit=1)['expired'] == 1
    assert media.cleanup(limit=1)['expired'] == 1
    assert not media.capacity()['over_limit']
    assert media.read(values[0][0], values[0][1], values[0][4], guard=lambda: None) == values[0][5]


def test_capacity_setting_backup_and_management_lock(service):
    assert call(service, 'media.capacity')['result']['mib'] == 2048
    request = secrets.token_hex(16)
    args = {'mib': 512, 'expected': None}
    saved = call(service, 'media.capacity.configure', args, request_id=request)
    assert saved['ok']
    assert call(service, 'media.capacity.configure', args, request_id=request) == saved
    assert call(service, 'media.capacity.configure', args)['error'] == 'edit_conflict'
    assert call(service, 'media.capacity.configure', {'mib': True, 'expected': saved['result']['revision']})['error'] == 'media_invalid'
    key = secrets.token_bytes(32)
    blob = postgres_transfer.create(service.store, key)
    MediaOriginals(service.store).configure_capacity(2048, saved['result']['revision'], actor='test')
    postgres_transfer.restore(service.store, blob, key, expected=postgres_transfer.fingerprint(service.store))
    assert MediaOriginals(service.store).capacity_settings()['mib'] == 512
    service.enable_lock('test-password-123456')
    service.lock()
    assert call(service, 'media.capacity')['error'] == 'desktop_locked'
    assert call(service, 'media.capacity.configure', args)['error'] == 'desktop_locked'


def test_task_hold_supports_configurable_multi_message_attachment_count(library):
    media, source = library
    values = [source(number, 1) for number in range(1, 14)]
    for args in values:
        put(media, args)
        finish(media, args)
    media.hold('owner', 'task', [args[4] for args in values])
    assert media.capacity()['protected_bytes'] == 13
    media.release('owner', 'task')
    assert media.capacity()['protected_bytes'] == 0


def test_merged_non_anchor_receipt_starts_retention_and_replay_does_not_extend(library):
    media, source = library
    args = source(1, 100)
    put(media, args)
    receipt = {'version': args[3], 'batch': 'synthetic', 'coverage': 'omitted', 'outcome': 'succeeded'}
    with pytest.raises(RuntimeError), media.store.db() as db:
        media.store.set_state('merged-consumed:' + args[2], receipt, db)
        raise RuntimeError('rollback before batch commit')
    assert media.listing()['items'][0]['processed_at'] is None
    media.store.set_state('merged-consumed:' + args[2], receipt)
    stamp = media.listing()['items'][0]['processed_at']
    assert stamp is not None
    media.store.set_state('merged-consumed:' + args[2], receipt)
    assert media.listing()['items'][0]['processed_at'] == stamp
    later = source(2, 100)
    media.store.set_state('merged-consumed:' + later[2], {**receipt, 'version': later[3]})
    assert put(media, later)['processed_at'] is not None
