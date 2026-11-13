import hashlib
import secrets
from dataclasses import replace

import pytest

from companion_core import events, postgres_transfer
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.media_originals import MediaOriginals
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


@pytest.fixture
def sample(pg_store):
    scope = ReplyScope('qq', '10001', 'group', '10003')
    permission = ReplyAuthorizations(pg_store).update(scope, enabled=True, expected_version=None, actor='test')['version']
    identifier = stable_key('qq-native-attachment', 'test-file')
    event = Event('qq', '10001', 'media-test', ActorRef('qq', '10002', '10001'), 'group:10003',
                  '2026-10-31T00:00:00+00:00', '文件尚未读取', 'group',
                  attachments=({'id': identifier, 'kind': 'file', 'position': 0},))
    version = events.ingest(pg_store, event)['version']
    media = MediaOriginals(pg_store)
    content = '正文中文🙂'.encode()
    args = (scope, permission, event.id, version, identifier, content)
    return media, args, event


def put(sample):
    media, args, _ = sample
    return media.put(*args, name='样本.txt', guard=lambda: None)


def finish(sample):
    media, args, _ = sample
    media.store.set_state('core-processed:' + args[2], args[3])
    return media.listing()['items'][0]['processed_at']


def test_dead_process_hold_recovered_but_live_owner_and_pending_original_protected(sample):
    import os
    import subprocess
    import sys

    media, args, _ = sample
    put(sample)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        owner = f'qiban-media:{child.pid}:synthetic-owner'
        media.hold(owner, 'test', [args[4]])
        media.clock = lambda: 9999999999
        assert media.cleanup()['expired'] == 0
        child.kill()
        child.wait(timeout=5)
        assert media.cleanup()['expired'] == 0  # Source still pending.
        with media.store.db() as db:
            assert db.execute('SELECT count(*) FROM media_original_holds').fetchone()[0] == 0
        live = f'qiban-media:{os.getpid()}:current-owner'
        media.hold(live, 'test', [args[4]])
        finish(sample)
        assert media.cleanup()['expired'] == 0
        media.release(live, 'test')
        assert media.cleanup()['expired'] == 1
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_encrypted_bound_to_source_and_idempotent(sample):
    media, args, _ = sample
    first = put(sample)
    assert first['processed_at'] is None and put(sample)['revision'] == first['revision']
    assert media.read(args[0], args[1], args[4], guard=lambda: None) == args[5]
    with media.store.db() as db:
        row = db.execute('SELECT body,metadata FROM media_originals').fetchone()
        assert args[5] not in bytes(row[0]) and row[1].startswith('enc1:') and '样本' not in row[1]
    assert media.settings() == {'days': 30, 'revision': None}


def test_cleanup_only_after_durable_receipt_and_keeps_event(sample):
    media, args, event = sample
    put(sample)
    media.clock = lambda: 9999999999
    assert media.cleanup() == {'expired': 0}
    stamp = finish(sample)
    media.clock = lambda: stamp + 30 * 86400 - 1
    assert media.cleanup()['expired'] == 0
    media.clock = lambda: stamp + 30 * 86400
    assert media.cleanup()['expired'] == 1
    assert media.cleanup()['expired'] == 0
    assert events.get(media.store, event.id)['event']['text'] == event.text
    assert media.store.state('core-processed:' + event.id) == args[3]
    assert media.listing()['items'][0]['status'] == 'expired'
    with pytest.raises(AssistantError, match='原件已'):
        media.read(args[0], args[1], args[4], guard=lambda: None)
    with pytest.raises(AssistantError):
        put(sample)  # No silent redownload/resurrection after expiry.


@pytest.mark.parametrize('reason', ['saved', 'favorite', 'emoticon'])
def test_preservation_and_task_hold_survive_restart(sample, reason):
    media, args, _ = sample
    first = put(sample)
    pinned = media.preserve(args[4], [reason], first['revision'], actor='admin')
    stamp = finish(sample)
    media = MediaOriginals(media.store, clock=lambda: stamp + 400 * 86400)
    assert media.cleanup()['expired'] == 0
    media.hold('runtime-owner', 'pending-task', [args[4]])
    media.preserve(args[4], [], pinned['revision'], actor='admin')
    assert MediaOriginals(media.store, clock=media.clock).cleanup()['expired'] == 0
    media.release('different-owner', 'pending-task')
    assert media.cleanup()['expired'] == 0
    media.release('runtime-owner', 'pending-task')
    assert media.cleanup()['expired'] == 1


def test_setting_cas_changes_existing_retention_and_never_accepts_bool(sample):
    media, _, _ = sample
    put(sample)
    stamp = finish(sample)
    media.clock = lambda: stamp + 15 * 86400
    assert media.cleanup()['expired'] == 0
    saved = media.configure(10, None, actor='admin')
    assert saved['days'] == 10
    for invalid in (True, 0, -1, 3651, '30'):
        with pytest.raises(AssistantError):
            media.configure(invalid, saved['revision'], actor='admin')
    with pytest.raises(AssistantError):
        media.configure(30, None, actor='admin')
    assert media.cleanup()['expired'] == 1


def test_source_changes_revocation_and_wrong_scope_deny_reads(sample):
    media, args, event = sample
    put(sample)
    other = ReplyScope('qq', '10001', 'group', '10004')
    version = ReplyAuthorizations(media.store).update(other, enabled=True, expected_version=None, actor='test')['version']
    with pytest.raises(AssistantError):
        media.read(other, version, args[4], guard=lambda: None)
    events.ingest(media.store, replace(event, text='changed'))
    with pytest.raises(AssistantError):
        media.read(args[0], args[1], args[4], guard=lambda: None)
    ReplyAuthorizations(media.store).update(args[0], enabled=False, expected_version=args[1], actor='test')
    with pytest.raises(AssistantError):
        media.read(args[0], args[1], args[4], guard=lambda: None)


def test_cancel_before_commit_rolls_back_original(sample):
    media, args, _ = sample
    calls = []
    def guard():
        calls.append(1)
        if len(calls) == 2:
            raise AssistantError('cancelled', 'cancelled')
    with pytest.raises(AssistantError):
        media.put(*args, name='test', guard=guard)
    assert not media.listing()['items']


def test_receipt_and_cleanup_transactions_rollback_and_replay(sample):
    media, args, _ = sample
    put(sample)
    with pytest.raises(RuntimeError), media.store.lock, media.store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        media.store.set_state('core-processed:' + args[2], args[3], db)
        raise RuntimeError('simulated interruption')
    assert media.listing()['items'][0]['processed_at'] is None
    stamp = finish(sample)
    assert finish(sample) == stamp
    media.clock = lambda: stamp + 31 * 86400
    with pytest.raises(RuntimeError), media.store.lock, media.store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        assert media.cleanup()['expired'] == 1
        raise RuntimeError('simulated power loss before commit')
    assert media.read(args[0], args[1], args[4], guard=lambda: None) == args[5]
    assert media.cleanup()['expired'] == 1


def test_backup_restore_includes_originals_and_clears_exited_task_holds(sample):
    media, args, _ = sample
    first = put(sample)
    media.preserve(args[4], ['favorite'], first['revision'], actor='admin')
    media.configure(45, None, actor='admin')
    media.hold('old-runtime', 'old-task', [args[4]])
    key = secrets.token_bytes(32)
    backup = postgres_transfer.create(media.store, key)
    assert args[5] not in backup
    postgres_transfer.restore(media.store, backup, key, expected=postgres_transfer.fingerprint(media.store))
    assert media.settings()['days'] == 45
    assert media.listing()['items'][0]['preserve'] == ['favorite']
    with media.store.db() as db:
        assert db.execute('SELECT count(*) FROM media_original_holds').fetchone()[0] == 0
        row = db.execute('SELECT * FROM media_originals').fetchone()
        assert hashlib.sha256(media._decode(row)).hexdigest() == row['sha256']


def test_expired_backup_does_not_restore_original_bytes(sample):
    media, args, _ = sample
    put(sample)
    stamp = finish(sample)
    media.clock = lambda: stamp + 31 * 86400
    media.cleanup()
    key = secrets.token_bytes(32)
    backup = postgres_transfer.create(media.store, key)
    postgres_transfer.restore(media.store, backup, key, expected=postgres_transfer.fingerprint(media.store))
    with media.store.db() as db:
        assert db.execute('SELECT body FROM media_originals').fetchone()[0] is None


def test_tampered_blob_restore_rejected_without_changing_current_store(sample):
    media, _, _ = sample
    put(sample)
    with media.store.db() as db:
        db.execute("UPDATE media_originals SET body=?", (b'corrupt',))
    key = secrets.token_bytes(32)
    backup = postgres_transfer.create(media.store, key)
    before = postgres_transfer.fingerprint(media.store)
    with pytest.raises(AssistantError):
        postgres_transfer.restore(media.store, backup, key, expected=before)
    assert postgres_transfer.fingerprint(media.store) == before


def test_desktop_management_settings_request_deduplication_and_lock(service):
    assert call(service, 'media.settings')['result']['days'] == 30
    request = secrets.token_hex(16)
    args = {'days': 45, 'expected': None}
    result = call(service, 'media.configure', args, request_id=request)
    assert result['ok'] and result['result']['days'] == 45
    assert call(service, 'media.configure', args, request_id=request) == result
    assert not call(service, 'media.configure', {'days': 20, 'expected': None})['ok']
    assert not call(service, 'media.configure', {'days': True, 'expected': result['result']['revision']})['ok']
    assert call(service, 'media.list', {'after': '', 'limit': 50})['result']['items'] == []
    service.enable_lock('test-password-123456')
    service.lock()
    for op, args in [('media.settings', {}), ('media.list', {'after': '', 'limit': 50}),
                     ('media.configure', {'days': 1, 'expected': result['result']['revision']})]:
        assert call(service, op, args)['error'] == 'desktop_locked'


def test_invalid_source_and_cancel_during_cleanup_cannot_remove_anything(sample):
    media, args, _ = sample
    with pytest.raises(AssistantError):
        media.put(*args[:4], stable_key('qq-native-attachment', 'not-in-event'), args[5], name='test', guard=lambda: None)
    put(sample)
    stamp = finish(sample)
    media.clock = lambda: stamp + 31 * 86400
    count = []
    def guard():
        count.append(1)
        if len(count) == 2:
            raise AssistantError('stale_runtime', 'stale')
    with pytest.raises(AssistantError):
        media.cleanup(guard=guard)
    assert media.read(args[0], args[1], args[4], guard=lambda: None) == args[5]
