import io
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from companion_core import conversations, events
from companion_core.access import RecordPolicy
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import GovernedContext
from tests_postgres.test_web import client, login
from tests_postgres.test_web import service as service_fixture

service = service_fixture


def seed(service, count, *, other=False):
    store = service.store
    actor = ActorRef('local', 'other') if other else service.sessions.actor(service.sessions.request(service.sessions.login('synthetic-password-2026', 'role'), 'role'))
    account = service.local.account
    for index in range(count):
        at = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat()
        event = Event('local', account, f'{actor.key}-{index}', actor, actor.key, at, f'message {index} needle')
        events.ingest(store, event)
        governed = GovernedContext(store, event)
        governed.classify_event()
        conversations.append(store, account, 'local', event.thread, event.id, event.text, 'user', actor.key, 'received', at, at,
            on_write=lambda key, g=governed, e=event: g.classify_written('conversation_turns', key, sources=['event/'+e.id]))


def test_scoped_pages_search_cursor_revocation(service, monkeypatch):
    role = client(service, 'role')
    headers = login(role, service, 'role')
    seed(service, 105)
    seed(service, 200, other=True)
    decoded = []
    original = service.store._decode

    def counted(kind, *args):
        if kind == 'conversation_turns':
            decoded.append(args[0])
        return original(kind, *args)

    monkeypatch.setattr(service.store, '_decode', counted)
    page = role.post('/conversation', json={'limit': 30}, headers=headers).json()
    assert len(page['messages']) == 30 and len(decoded) == 30
    assert page['messages'][0]['text'] == 'message 75 needle'
    second = role.post('/conversation', json={'before': page['next'], 'limit': 30}, headers=headers).json()
    assert second['messages'][-1]['text'] == 'message 74 needle'
    assert not {i['id'] for i in page['messages']} & {i['id'] for i in second['messages']}
    found = role.post('/conversation', json={'query': 'message 12 '}, headers=headers).json()
    assert [i['text'] for i in found['messages']] == ['message 12 needle']
    assert role.post('/conversation', json={'after': 'bad'}, headers=headers).status_code == 403
    key = page['messages'][0]['id']
    item = service.store.read('conversation_turns', key)
    service.store.write('conversation_turns', {**item['data'], 'content': 'corrected'}, item['version'], 'admin-fixture', key)
    corrected = role.post('/conversation', json={'after': page['newest'], 'known': [key],
        'known_versions': {key: item['version']}}, headers=headers).json()
    assert corrected['updates'][0]['text'] == 'corrected'
    p = service.sessions.repository.policy('conversation_turns/' + key)
    service.sessions.repository.set_policy(replace(p, valid=False, version=secrets.token_hex(16)), p.version, 'test')
    changed = role.post('/conversation', json={'after': page['newest'], 'known': [key]}, headers=headers).json()
    assert changed['removed'] == [key] and changed['messages'] == []


@pytest.mark.asyncio
async def test_cancel_queued_inflight_and_completed(service):
    role = client(service, 'role')
    headers = login(role, service, 'role')
    service.control('synthetic-bridge-secret', 'control', 'assistant_start', {})
    identifier = role.post('/messages', json={'text': 'cancel me', 'nonce': secrets.token_hex(16)}, headers=headers).json()['id']
    assert role.get('/conversation').json()[0]['status'] == 'queued'
    assert role.post('/cancel-message', json={'id': identifier}, headers=headers).status_code == 200
    await service.drain_local()
    assert role.get('/conversation').json()[0]['reason'] == 'user_cancelled'
    original = service.core.decisions.model.generate

    async def cancelled(*args, **kwargs):
        assert role.post('/cancel-message', json={'id': current}, headers=headers).status_code == 200
        return await original(*args, **kwargs)

    service.core.decisions.model.generate = cancelled
    current = role.post('/messages', json={'text': 'in flight', 'nonce': secrets.token_hex(16)}, headers=headers).json()['id']
    await service.drain_local()
    service.core.decisions.model.generate = original
    assert all(i['speaker'] != 'assistant' for i in role.get('/messages').json())
    current = role.post('/messages', json={'text': 'complete', 'nonce': secrets.token_hex(16)}, headers=headers).json()['id']
    await service.drain_local()
    assert role.post('/cancel-message', json={'id': current}, headers=headers).status_code == 403


def picture():
    output = io.BytesIO()
    Image.new('RGB', (24, 18), '#e0a000').save(output, format='PNG')
    return output.getvalue()


@pytest.mark.asyncio
async def test_encrypted_image_scope_model_cleanup_and_revocation(service):
    role = client(service, 'role')
    headers = login(role, service, 'role')
    assert role.post('/input-image', content=picture(), headers={'origin': 'http://127.0.0.1:45181'}).status_code == 403
    upload = role.post('/input-image', content=picture(), headers=headers)
    assert upload.status_code == 201, upload.text
    key = upload.json()['id']
    assert role.get('/input-image?id='+key).content == picture()
    with service.store.db() as db:
        row = db.execute('SELECT body FROM input_image_blobs WHERE id=?', (key,)).fetchone()
        assert picture() not in bytes(row[0])
    service.control('synthetic-bridge-secret', 'control', 'assistant_start', {})
    original = service.core.decisions.model.generate
    seen = []

    async def capture(prompt, context, schema, images=(), **kwargs):
        if 'event_material' in context:
            assert context['event_material']['images_read'] == 1
            seen.extend(images)
            assert images[0].read_bytes() == picture()
        return await original(prompt, context, schema, images, **kwargs)

    service.core.decisions.model.generate = capture
    sent = role.post('/messages', json={'text': 'what color?', 'nonce': secrets.token_hex(16), 'images': [key]}, headers=headers)
    assert sent.status_code == 202, sent.text
    await service.drain_local()
    assert seen and all(not path.exists() for path in seen)
    assert role.delete('/input-image?id='+key, headers=headers).status_code == 403
    p = service.sessions.repository.policy('input_images/' + key)
    service.sessions.repository.set_policy(replace(p, valid=False, version=secrets.token_hex(16)), p.version, 'test')
    assert role.get('/input-image?id='+key).status_code == 403
    assert role.get('/messages').json() == []
    assert role.post('/input-image', content=b'<svg/>', headers=headers).status_code == 403


def test_foreign_image_and_scope_index_cannot_grant_access(service):
    role = client(service, 'role')
    headers = login(role, service, 'role')
    key = role.post('/input-image', content=picture(), headers=headers).json()['id']
    p = service.sessions.repository.policy('input_images/' + key)
    service.sessions.repository.set_policy(RecordPolicy(p.resource, secrets.token_hex(16), 'public'), p.version, 'test')
    other = ActorRef('local', 'stranger')
    request = service.sessions.authority.issue(actor=other, conversation=p.conversation, audience='private', recipients=(other,))
    with pytest.raises(AssistantError, match='不属于'):
        service.local.images.read(request, key)


def test_input_image_backup_restore_and_integrity(service):
    from companion_core.input_images import verify_blobs
    from companion_core.postgres_transfer import create, fingerprint, restore

    token = service.sessions.login('synthetic-password-2026', 'role')
    request = service.sessions.request(token, 'role')
    key = service.local.images.upload(request, picture())['id']
    blob = create(service.store, service.store._backup_key)
    service.local.images.discard(request, key)
    restore(service.store, blob, service.store._backup_key, expected=fingerprint(service.store))
    token = service.sessions.login('synthetic-password-2026', 'role')
    request = service.sessions.request(token, 'role')
    assert service.local.images.read(request, key) == picture()
    with service.store.db() as db:
        db.execute('DELETE FROM input_image_blobs WHERE id=?', (key,))
    with pytest.raises(AssistantError, match='不完整'):
        verify_blobs(service.store)


def test_image_normalization_applies_orientation_and_transparency(service):
    token = service.sessions.login('synthetic-password-2026', 'role')
    request = service.sessions.request(token, 'role')
    output = io.BytesIO()
    Image.new('RGBA', (8, 4), (255, 0, 0, 0)).save(output, format='PNG')
    key = service.local.images.upload(request, output.getvalue())['id']
    with Image.open(io.BytesIO(service.local.images.read(request, key))) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)
    output = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new('RGB', (8, 4), 'blue').save(output, format='JPEG', exif=exif)
    saved = service.local.images.upload(request, output.getvalue())
    assert (saved['width'], saved['height']) == (4, 8)
    with Image.open(io.BytesIO(service.local.images.read(request, saved['id']))) as image:
        assert not image.getexif()
