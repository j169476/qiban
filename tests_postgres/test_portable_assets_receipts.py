"""A portable stage carries registered assets, the send ledger and the room's own history.

These tests drive the real staging path (``portable_restore.stage_restore``) against the
disposable installation from ``tests_postgres/test_portable_staging.py`` and open the
generation it selects through the storage worker. Everything written here is synthetic:
no model is called, no platform account is used, no network request is made and no
message is sent anywhere.

Not covered here: Windows administrator verification, the native stager pipe, the
in-app portable export/preview flow, credential-slot rotation beyond the slot the worker
opens with, and asset retention or cleanup after the switch.

The ``prepared`` ledger rows are seeded with the documented columns and the store's own
``ledger_encode`` bodies, exactly as ``tests_postgres/test_startup_recovery.py`` and
``tests_postgres/test_desktop_runtime.py`` do: the only live writer of a prepared action
is the retired B站 scheduler, which this installation never starts. Every other ledger
and receipt record below is written through a product API.
"""
import hashlib
import io
import json
import secrets
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'desktop_probe'))

import installation_generation  # noqa: E402
import portable_restore  # noqa: E402

from companion_core import asset_transfer, events, postgres_transfer  # noqa: E402
from companion_core.contracts import ActorRef, Event, stable_key  # noqa: E402
from companion_core.file_library import FileLibrary, verify_files  # noqa: E402
from companion_core.media_originals import MediaOriginals, verify_originals  # noqa: E402
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope  # noqa: E402
from companion_core.sticker_library import StickerLibrary, verify_stickers  # noqa: E402
from qiban import desktop_identity  # noqa: E402
from qiban.desktop_management import LOCK, OWNER, DesktopManagement  # noqa: E402
from qiban.desktop_portable import PRESERVATION  # noqa: E402
from qiban.desktop_runtime import DesktopRuntime  # noqa: E402
from tests_postgres.test_desktop_management import call  # noqa: E402
from tests_postgres.test_portable_staging import (  # noqa: E402
    SESSION_ID,
    WINDOWS_SID,
    generations,
    machine,
    require_entry,
    root,
    staging,
)
from tests_postgres.test_sticker_library import picture  # noqa: E402
from tests_postgres.test_wechat_role import Model  # noqa: E402

__all__ = ['machine', 'require_entry', 'root', 'staging']

SCOPE = ReplyScope('qq', '10001', 'group', '10003')


def png(color):
    buffer = io.BytesIO()
    Image.new('RGB', (12, 9), color).save(buffer, format='PNG')
    return buffer.getvalue()


def reference(store, filename, color, description):
    """Register one reference image the way the persona editor does: file plus digest."""
    raw = png(color)
    path = store.root / 'assets' / 'persona' / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    persona = store.read('persona')
    digest = hashlib.sha256(raw).hexdigest()
    store.write('persona', {**persona['data'], 'visual_identity': {
        'description': description,
        'references': [{'filename': filename, 'sha256': digest, 'label': 'synthetic'}]}},
        persona['version'], 'synthetic')
    return path, digest


def ledger(store, status):
    """One durable send record per ledger, in a state only an interrupted send leaves."""
    with store.db() as db:
        db.execute('INSERT INTO core_actions VALUES(?,?,?,?,?,?,?,?,?,?)', (
            'core-' + status, 'core-' + status, 'source-generation', 'qq', 'synthetic', 'message.reply',
            store.ledger_encode('core_actions.body', {'synthetic': True}), status, None,
            '2026-11-07T00:00:00+00:00'))
        db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            'legacy-' + status, 'legacy-' + status, 'synthetic', 'source-generation', 'synthetic', 'reply',
            store.ledger_encode('actions.payload', {'synthetic': True}), status, '2026-11-07', 1, None,
            '2026-11-07T00:00:00+00:00', None, None))


def receipt(store, event, status):
    """A processed receipt and its delivery record, written the way the core writes them."""
    version = events.ingest(store, event)['version']
    store.set_state('core-processed:' + event.id, version)
    store.set_state('core-delivery:' + event.id, {
        'version': version, 'owner': 'source-control', 'status': status,
        'due_at': '2026-11-08T00:00:00+00:00' if status == 'deferred' else None,
        'config_token': 'synthetic-config'})
    return version


def table(store, query):
    with store.db() as db:
        return [tuple(row) for row in db.execute(query)]


def event_scope(store, identifier):
    """The stored scope of one event: actor key, account and the conversation key.

    ``events.ingest`` writes ``event.thread`` (its own conversation stable key) into
    ``core_events.conversation``, not the actor key, so this reads the rows instead of
    assuming the identity behind them.
    """
    with store.db() as db:
        row = db.execute('SELECT actor,account,conversation FROM core_events WHERE id=?',
                         (identifier,)).fetchone()
    return tuple(row) if row else None


def message(native_id, text):
    return Event('qq', SCOPE.account_identity, native_id, ActorRef('qq', '10002', SCOPE.account_identity),
                 'group:' + SCOPE.target_id, '2026-11-07T00:00:00+00:00', text, 'group')


def test_registered_reference_image_and_source_history_arrive_in_the_new_generation(machine):
    source_file, digest = reference(machine.source, 'source-reference.png', 'blue', '来源机器的参考形象')
    current_file, current_digest = reference(machine.current, 'current-reference.png', 'green', '本机旧参考形象')
    source_bytes, current_bytes = source_file.read_bytes(), current_file.read_bytes()
    result, preservation, fingerprint = machine.staged()
    settings, store = machine.opened()
    try:
        assert settings['generation'] == result['generation']
        staged = Path(result['store']) / 'assets' / 'persona' / 'source-reference.png'
        assert staged.is_relative_to(Path(result['store'])) and staged.is_file()
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == digest
        # The product's own asset capture authenticates the staged file and its registration.
        assert [(item['path'], item['sha256']) for item in asset_transfer.capture(store)] == [
            ('assets/persona/source-reference.png', digest)]
        # The source history still decrypts byte for byte inside the new generation.
        history = table(store, "SELECT id,version,body FROM content_history WHERE kind='memories'")
        assert len(history) == 1 and history[0][0] == 'source-memory'
        old = store._decode('memories', history[0][0], history[0][1], history[0][2])
        assert old['data']['content'] == '来源机器的旧记忆'
        assert store.read('memories', 'source-memory')['data']['content'] == '来源机器的旧记忆（续写）'
        # Neither the source machine nor this machine's own data moved.
        assert source_file.read_bytes() == source_bytes
        assert hashlib.sha256(source_file.read_bytes()).hexdigest() == digest
        assert postgres_transfer.fingerprint(machine.current) == fingerprint
        assert current_file.read_bytes() == current_bytes
        assert hashlib.sha256(current_file.read_bytes()).hexdigest() == current_digest
        assert not (machine.current.root / 'assets' / 'persona' / 'source-reference.png').exists()
        artifact = machine.root / 'store' / 'history' / PRESERVATION / preservation['name']
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == preservation['sha256']
    finally:
        store.database.close()


def test_file_sticker_and_attachment_originals_are_readable_in_the_new_generation(machine):
    source = machine.source
    authorization = ReplyAuthorizations(source).update(SCOPE, enabled=True, expected_version=None,
                                                       actor='synthetic')['version']
    document = '便携原件正文🙂'.encode()
    library = FileLibrary(source)
    file_item = library.import_bytes(document, '便携说明.txt', ['便携'], actor='synthetic')['item']
    file_item = library.configure(file_item['id'], file_item['name'], file_item['tags'], True,
                                  [asdict(SCOPE)], file_item['revision'], actor='synthetic')
    content = picture()
    stickers = StickerLibrary(source)
    sticker_item = stickers.import_bytes(content, '便携表情', ['便携'], actor='synthetic')['item']
    sticker_item = stickers.configure(sticker_item['id'], sticker_item['name'], sticker_item['tags'], True,
                                      sticker_item['revision'], actor='synthetic')
    attachment = '便携附件正文🙂'.encode()
    identifier = stable_key('qq-native-attachment', 'portable-sample')
    event = replace(message('portable-media', '合成附件尚未读取'),
                    attachments=({'id': identifier, 'kind': 'file', 'position': 0},))
    version = events.ingest(source, event)['version']
    MediaOriginals(source).put(SCOPE, authorization, event.id, version, identifier, attachment,
                               name='便携附件.txt', guard=lambda: None)
    result, _, _ = machine.staged()
    settings, store = machine.opened()
    try:
        # The restore's own verification decrypts every registered original in the new root.
        verify_files(store)
        verify_stickers(store)
        verify_originals(store)
        file_row, = table(store, 'SELECT id,sha256,size,revision FROM file_library')
        sticker_row, = table(store, 'SELECT id,sha256,size,revision FROM sticker_library')
        assert file_row[0] == file_item['id'] and file_row[1] == hashlib.sha256(document).hexdigest()
        assert file_row[2] == len(document)
        assert sticker_row[0] == sticker_item['id'] and sticker_row[1] == hashlib.sha256(content).hexdigest()
        assert sticker_row[2] == len(content)
        # This installation re-authorises the restored conversation and reads the exact bytes.
        expected = ReplyAuthorizations(store).record(SCOPE)['version']
        allowed = ReplyAuthorizations(store).update(SCOPE, enabled=True, expected_version=expected,
                                                    actor='synthetic')['version']
        assert FileLibrary(store).read_for_send(SCOPE, allowed, file_row[0], file_row[3],
                                                guard=lambda: None) == document
        assert StickerLibrary(store).read_for_send(sticker_row[0], sticker_row[3], guard=lambda: None) == content
        assert MediaOriginals(store).read(SCOPE, allowed, identifier, guard=lambda: None) == attachment
        # The source machine keeps its library, and this machine's own library was never involved.
        assert table(source, 'SELECT id,sha256 FROM file_library') == [(file_item['id'], file_row[1])]
        assert table(source, 'SELECT id,sha256 FROM sticker_library') == [(sticker_item['id'], sticker_row[1])]
        assert table(machine.current, 'SELECT count(*) FROM file_library') == [(0,)]
        assert table(machine.current, 'SELECT count(*) FROM sticker_library') == [(0,)]
    finally:
        store.database.close()


def test_a_failed_asset_write_only_abandons_the_new_generation(machine, monkeypatch):
    source_file, digest = reference(machine.source, 'source-reference.png', 'blue', '来源机器的参考形象')
    event = message('portable-failure', '便携失败用例来源消息')
    events.ingest(machine.source, event)
    preservation, fingerprint = machine.preserved()
    blob = machine.exported()
    before = generations(machine.root)
    original = asset_transfer.atomic_write

    def fail(*args, **kwargs):
        raise OSError('synthetic asset write failure')

    monkeypatch.setattr(asset_transfer, 'atomic_write', fail)
    with pytest.raises(OSError):
        portable_restore.stage_restore(current_store=machine.current, root=machine.root, source_blob=blob,
                                       source_key=machine.source_key, expected_current_fingerprint=fingerprint,
                                       preservation=preservation, application_session=SESSION_ID)
    monkeypatch.setattr(asset_transfer, 'atomic_write', original)
    # Nothing is selected, and this machine's own data is exactly what the preview authenticated.
    selection = installation_generation.load_selection(machine.root, machine.installation)
    assert selection['generation'] is None and selection['database'] == 'qiban'
    pointer = machine.root / 'active-generation.json'
    assert (pointer.read_bytes() if pointer.exists() else None) == machine.staging.saved_pointer
    assert postgres_transfer.fingerprint(machine.current) == fingerprint
    artifact = machine.root / 'store' / 'history' / PRESERVATION / preservation['name']
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == preservation['sha256']
    assert source_file.is_file() and hashlib.sha256(source_file.read_bytes()).hexdigest() == digest
    # The abandoned generation keeps its directory, journal and database as evidence only.
    abandoned, = sorted(generations(machine.root) - before)
    journal = json.loads((machine.root / 'generations' / abandoned / 'restore-journal.json')
                         .read_text(encoding='utf8'))
    assert journal['phase'] == 'failed' and journal['generation'] == abandoned
    assert not (machine.root / 'generations' / abandoned / 'store' / 'assets' / 'persona'
                / 'source-reference.png').exists()
    # A manual retry stages one fresh generation and never repeats the source message.
    result, _, _ = machine.staged()
    assert result['generation'] != abandoned and result['switched'] is True
    settings, store = machine.opened()
    try:
        staged = Path(result['store']) / 'assets' / 'persona' / 'source-reference.png'
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == digest
        assert events.get(store, event.id)['event']['text'] == '便携失败用例来源消息'
        assert table(store, 'SELECT count(*) FROM core_events') == [(1,)]
        assert table(store, 'SELECT count(*) FROM core_actions') == [(0,)]
        assert table(store, 'SELECT count(*) FROM actions') == [(0,)]
        assert table(machine.source, 'SELECT count(*) FROM core_events') == [(1,)]
    finally:
        store.database.close()


def test_unfinished_sends_are_classified_and_receipts_survive_the_stage(machine):
    source = machine.source
    confirmed = message('portable-confirmed', '已确认发送的来源消息')
    deferred = replace(confirmed, native_id='portable-deferred', text='待重试的来源消息')
    versions = {confirmed.id: receipt(source, confirmed, 'completed'),
                deferred.id: receipt(source, deferred, 'deferred')}
    for status in ('prepared', 'dispatching', 'confirmed', 'uncertain'):
        ledger(source, status)
    expected_receipts = table(source, 'SELECT event_id,version FROM core_event_receipts ORDER BY event_id')
    expected_delivery = table(source, 'SELECT event_id,version,owner,status,due_at FROM core_delivery_state '
                                      'ORDER BY event_id')
    assert sorted(expected_receipts) == sorted(versions.items())
    result, _, _ = machine.staged()
    settings, store = machine.opened()
    try:
        # An unfinished send is never replayed: prepared is cancelled, dispatching is uncertain,
        # and a confirmed send stays exactly as it was.
        assert {row[0]: row[1] for row in table(store, 'SELECT id,status FROM core_actions')} == {
            'core-prepared': 'cancelled', 'core-dispatching': 'uncertain',
            'core-confirmed': 'confirmed', 'core-uncertain': 'uncertain'}
        assert {row[0]: row[1] for row in table(store, 'SELECT id,status FROM actions')} == {
            'legacy-prepared': 'cancelled', 'legacy-dispatching': 'uncertain',
            'legacy-confirmed': 'confirmed', 'legacy-uncertain': 'uncertain'}
        # Real processing receipts and delivery records travel unchanged with the generation.
        assert table(store, 'SELECT event_id,version FROM core_event_receipts ORDER BY event_id') == expected_receipts
        assert table(store, 'SELECT event_id,version,owner,status,due_at FROM core_delivery_state '
                            'ORDER BY event_id') == expected_delivery
        for event_id, version in versions.items():
            assert store.state('core-processed:' + event_id) == version
        assert store.state('core-delivery:' + deferred.id)['status'] == 'deferred'
        assert events.get(store, confirmed.id)['event']['text'] == '已确认发送的来源消息'
    finally:
        store.database.close()


async def test_the_staged_room_keeps_its_history_and_continues_the_source_identity(machine):
    machine.source.set_state(LOCK, {'enabled': False, 'revision': 0, 'attempts': 0, 'until': 0})
    source = DesktopManagement(machine.source, machine.source_installation,
                               machine.source.state(OWNER)['windows_sid'])
    runtime = DesktopRuntime(source, 'portable-source-room', room_model=Model(), adapters=[source.room])
    try:
        runtime.start('local')
        posted = call(source, 'room.post', {'session': source.session_id, 'text': '来源会客室的旧消息',
                                            'nonce': secrets.token_hex(16)})
        assert posted['ok'] and posted['result']['new']
        # The source event's own stored scope: it must survive processing and the stage unchanged.
        source_event = event_scope(machine.source, posted['result']['id'])
        assert source_event is not None and source_event[0] == machine.source_owner.key
        # Only the synthetic model processes the turn; the room adapter sends nothing.
        event, = await source.room.runtime_poll()
        await source.room.core.handle(event)
        assert event_scope(machine.source, posted['result']['id']) == source_event
        assert '来源会客室的旧消息' in [turn['data']['content']
                                       for turn in machine.source.records('conversation_turns')[0]]
    finally:
        runtime.close()
    result, _, _ = machine.staged()
    settings, store = machine.opened()
    target = DesktopManagement(store, machine.installation, WINDOWS_SID)
    again = DesktopRuntime(target, 'portable-room-app', room_model=Model(), adapters=[target.room])
    try:
        again.start('local')
        token = target.session_id
        history = call(target, 'room.history', {'session': token, 'before': ''})['result']['messages']
        assert '来源会客室的旧消息' in [item['text'] for item in history]
        # The conversation turn itself arrived with the generation, not only the event row.
        assert '来源会客室的旧消息' in [turn['data']['content'] for turn in store.records('conversation_turns')[0]]
        # A new room message continues the bound source identity in the same conversation.
        posted = call(target, 'room.post', {'session': token, 'text': '恢复后的新消息',
                                            'nonce': secrets.token_hex(16)})
        assert posted['ok'] and posted['result']['new']
        # The new message repeats the source event's stored scope exactly: the same actor,
        # the same account and the same conversation key, all read from the source rows.
        fresh = event_scope(store, posted['result']['id'])
        assert fresh is not None and fresh == source_event
        assert fresh[0] == machine.source_owner.key
        assert fresh[1] == store.read('manifest')['data']['project_id']
        bound = desktop_identity.verify(store, installation=machine.installation, owner=target.actor)
        assert bound['room'] == machine.source_owner and bound['logical_role'] == machine.source_role
        # Management stays this machine's own Windows administrator, never the continued identity.
        assert store.state(OWNER)['windows_sid'] == WINDOWS_SID
        assert target.request().actor == target.actor.key and target.request().administrator is True
        assert call(target, 'session.status')['result']['windows_user_verified'] is True
    finally:
        again.close()
        store.database.close()
