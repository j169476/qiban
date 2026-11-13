import hashlib
import secrets
from pathlib import Path

import pytest

from companion_core import events
from companion_core.errors import AssistantError
from companion_core.media_budget import DEFAULTS, select
from companion_core.media_originals import MediaOriginals
from companion_core.reply_authorization import ReplyAuthorizations
from tests_postgres.test_qq_native_provider import native as native


async def test_formal_decision_material_reads_file_and_releases_hold(native, tmp_path):
    from companion_core.context_budget import pack

    candidate, anchor, content, downloads, stage = await prepare(native, tmp_path)
    async with native.module.decision_material(anchor, guard=native.core.guard) as material:
        assert len(downloads) == 1 and not list(stage.iterdir())
        doc = material['context']['attachment_texts'][0]
        assert doc['id'] == candidate['id'] and doc['units'][0]['text'] == content.decode()
        with native.store.db() as db:
            assert db.execute('SELECT count(*) FROM media_original_holds').fetchone()[0] == 1
        packed, _ = pack({'event_material': material['context']}, None)
        assert packed['event_material']['attachments_read'] == 1
        assert packed['event_material']['media_budget']['read'] == 1
        assert packed['event_material']['media_budget']['unread'] == 0
    with native.store.db() as db:
        assert db.execute('SELECT count(*) FROM media_original_holds').fetchone()[0] == 0
    assert not native.model.calls


async def test_full_capacity_skips_attachment_and_keeps_text_material(native, tmp_path, monkeypatch):
    _, anchor, _, _, _ = await prepare(native, tmp_path)

    def full(*args, **kwargs):
        raise AssistantError('media_capacity_full', 'full')

    monkeypatch.setattr(native.module.provider, 'save_attachment', full)
    async with native.module.decision_material(anchor, guard=native.core.guard) as material:
        assert material['context']['attachments_read'] == 0
        assert material['context']['media_budget']['reasons'][0]['code'] == 'media_capacity_full'
        assert events.get(native.store, anchor.id)['event']['text'] == anchor.text


@pytest.mark.parametrize('original_saved', [False, True])
async def test_received_message_continues_when_platform_history_disappears(native, tmp_path, original_saved):
    candidate, anchor, content, downloads, _ = await prepare(native, tmp_path)
    if original_saved:
        save(native, candidate)
    # Platform withdrawal/removal does not revoke the local conversation grant.
    # Only the previously ingested event and any verified original remain usable.
    native.workers[-1].messages = []
    batch = native.module.decision_batch(anchor)
    assert (await native.core.handle(anchor))['action']['status'] == 'succeeded'
    decisions = [c for c in native.model.calls if 'event_material' in c]
    assert len(decisions) == 1
    material = decisions[0]['event_material']
    assert events.get(native.store, anchor.id)['event']['text'] == anchor.text
    assert len(downloads) == int(original_saved)
    if original_saved:
        assert material['attachment_text']['items'][0]['units'][0]['text'] == content.decode()
        assert material['media_budget']['read'] == 1
        MediaOriginals(native.store).release('media-test-owner', 'media-test-task')
    else:
        assert material['media_budget']['read'] == 0
        assert any(r['code'] == 'qq_media_source_unavailable' for r in material['media_budget']['reasons'])
        assert not MediaOriginals(native.store).listing()['items']
    coverage = native.store.state(batch['key'])['coverage']
    assert coverage['media_budget']['read'] == int(original_saved)
    assert native.workers[-1].submits == 1  # Simulated transport only.


async def test_final_batch_receipt_matches_file_content_given_to_single_decision(native, tmp_path):
    _, anchor, content, _, _ = await prepare(native, tmp_path)
    batch = native.module.decision_batch(anchor)
    assert (await native.core.handle(anchor))['action']['status'] == 'succeeded'
    decisions = [c for c in native.model.calls if 'event_material' in c]
    assert len(decisions) == 1
    material = decisions[0]['event_material']
    assert material['attachment_text']['items'][0]['units'][0]['text'] == content.decode()
    coverage = native.store.state(batch['key'])['coverage']
    assert coverage['media_budget']['read'] == 1 and coverage['media_budget']['unread'] == 0
    unit = coverage['attachment_text']['items'][0]['units'][0]
    assert unit == {'source': {'line': 1}, 'start': 0, 'end': len(content.decode())}
    assert 'text' not in unit


@pytest.mark.parametrize('kind', ['file', 'audio'])
async def test_cancel_parser_stops_owned_work_before_releasing_hold(native, tmp_path, monkeypatch, kind):
    import asyncio
    import threading
    import time

    from companion_qq import native_documents

    _, anchor, _, _, _ = await prepare(native, tmp_path, kind=kind)
    started = threading.Event()

    def wait_parser(*args, guard, **kwargs):
        started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            guard()
            time.sleep(.01)
        pytest.fail('cancellation did not reach parser')

    monkeypatch.setattr(native_documents, 'parse_audio' if kind=='audio' else 'parse_file', wait_parser)

    async def enter():
        async with native.module.decision_material(anchor, guard=native.core.guard):
            pytest.fail('cancelled material must not be published')

    work = asyncio.create_task(enter())
    assert await asyncio.to_thread(started.wait, 3)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(work, 2)
    with native.store.db() as db:
        assert db.execute('SELECT count(*) FROM media_original_holds').fetchone()[0] == 0
    assert native.module.provider.closed and not native.model.calls


async def prepare(native, tmp_path, *, kind='file', content=None):
    env = native
    await env.module.runtime_poll()
    worker = env.workers[-1]
    worker.arrivals()
    content = '合成受管附件中文🙂'.encode() if content is None else content
    number, field = {'file': (3, 'fileElement'), 'image': (2, 'picElement'), 'audio': (4, 'pttElement')}[kind]
    worker.messages[0]['elements'].append({'elementType': number, 'elementId': '55',
        field: {'fileSize': str(len(content)), 'fileName': '合成.txt'}})
    await env.module.runtime_poll()
    anchor = (await env.module.runtime_poll())[0]
    batch = env.module.decision_batch(anchor)
    plan = select([events.get(env.store, identifier) for identifier, _ in batch['members']], DEFAULTS)
    candidate = plan['selected'][0]
    worker.config = {'data': str(tmp_path/'native-data')}
    stage = Path(worker.config['data'])/'media-staging'
    stage.mkdir(parents=True)
    worker.media_root = stage
    original = worker.request
    downloads = []
    worker.after_download = lambda: None

    def request(command, **args):
        if command != 'download_attachment':
            return original(command, **args)
        raw = worker.frozen[args['index']]
        assert args['source'] == {'msg_id': raw['msgId'], 'time': raw['msgTime'],
                                  'sender_uid': raw['senderUid'], 'element_id': raw['elements'][args['position']]['elementId']}
        assert args['max_bytes'] >= len(content)
        path = stage/(secrets.token_hex(16) + '.bin')
        path.write_bytes(content)
        downloads.append(args)
        result = {'kind': kind, 'path': str(path), 'bytes': len(content),
                  'sha256': hashlib.sha256(content).hexdigest()}
        worker.after_download()
        return result

    worker.request = request
    return candidate, anchor, content, downloads, stage


def save(env, candidate, **kwargs):
    return env.module.provider.save_attachment(env.module, candidate, max_bytes=1024*1024,
                                               owner='media-test-owner', task='media-test-task',
                                               guard=env.core.guard, **kwargs)


@pytest.mark.parametrize('kind', ['file', 'image', 'audio'])
async def test_fresh_native_locator_saved_encrypted_and_staging_removed(native, tmp_path, kind):
    candidate, anchor, content, downloads, stage = await prepare(native, tmp_path, kind=kind)
    worker = native.workers[-1]
    worker.messages[0]['msgId'] = '999999'
    worker.messages[0]['elements'][-1]['elementId'] = '777777'
    value = save(native, candidate)
    assert value['kind'] == kind and value['bytes'] == len(content) and 'path' not in value
    assert downloads[0]['source']['msg_id'] == '999999' and not list(stage.iterdir())
    media = MediaOriginals(native.store)
    scope = native.module.scope(anchor)
    permission = ReplyAuthorizations(native.store).version(scope)
    assert media.read(scope, permission, candidate['id'], guard=native.core.guard) == content
    with native.store.db() as db:
        assert content not in bytes(db.execute('SELECT body FROM media_originals').fetchone()[0])
        assert db.execute('SELECT count(*) FROM media_original_holds').fetchone()[0] == 1
    assert save(native, candidate)['revision'] == value['revision'] and len(downloads) == 1
    media.release('media-test-owner', 'media-test-task')
    assert not native.model.calls and not worker.submits


async def test_changed_canonical_source_never_downloads(native, tmp_path):
    candidate, _, _, downloads, _ = await prepare(native, tmp_path)
    native.workers[-1].messages[0]['elements'][0]['textElement']['content'] = 'source changed'
    with pytest.raises(AssistantError) as error:
        save(native, candidate)
    assert error.value.code == 'media_source_changed' and not downloads
    assert not MediaOriginals(native.store).listing()['items']


async def test_revocation_after_download_cleans_copy_without_publishing(native, tmp_path):
    candidate, anchor, _, downloads, stage = await prepare(native, tmp_path)
    scope = native.module.scope(anchor)
    permissions = ReplyAuthorizations(native.store)
    native.workers[-1].after_download = lambda: permissions.update(
        scope, enabled=False, expected_version=permissions.version(scope), actor='test')
    with pytest.raises(AssistantError):
        save(native, candidate)
    assert len(downloads) == 1 and not list(stage.iterdir())
    assert not MediaOriginals(native.store).listing()['items']


async def test_cancel_after_download_cleans_copy_without_publishing(native, tmp_path):
    candidate, _, _, downloads, stage = await prepare(native, tmp_path)
    native.workers[-1].after_download = native.core.stop
    with pytest.raises(AssistantError):
        save(native, candidate)
    assert len(downloads) == 1 and not list(stage.iterdir())
    assert not MediaOriginals(native.store).listing()['items']


async def test_expired_original_cannot_be_silently_downloaded_again(native, tmp_path):
    candidate, _, _, downloads, _ = await prepare(native, tmp_path)
    save(native, candidate)
    media = MediaOriginals(native.store)
    media.release('media-test-owner', 'media-test-task')
    native.store.set_state('core-processed:' + candidate['event_id'], candidate['source_version'])
    stamp = media.listing()['items'][0]['processed_at']
    media.clock = lambda: stamp + 31*86400
    assert media.cleanup()['expired'] == 1
    with pytest.raises(AssistantError) as error:
        save(native, candidate)
    assert error.value.code == 'media_expired' and len(downloads) == 1


async def test_formal_animation_material_reuses_original_and_records_frames(native, tmp_path):
    from tests.test_image_reader import animation

    candidate, anchor, _, downloads, _ = await prepare(native, tmp_path, kind='image', content=animation())
    async with native.module.decision_material(anchor, guard=native.core.guard) as material:
        assert len(material['images']) == 3 and all(isinstance(x, bytes) for x in material['images'])
        assert 'images' not in material['context']
        sources = material['context']['image_sources']
        assert sources[0]['id'] == candidate['id']
        assert [f['index'] for f in sources[0]['frames']] == [0, 2, 4]
        assert material['context']['attachments_read'] == 1
    batch = native.module.decision_batch(anchor)
    assert (await native.core.handle(anchor))['action']['status'] == 'succeeded'
    assert len(downloads) == 1
    coverage = native.store.state(batch['key'])['coverage']
    assert coverage['image_sources'] == sources
    assert coverage['media_budget']['image_frames'] == 3
    assert coverage['media_budget']['read'] == 1
    with native.store.db() as db:
        assert db.execute('SELECT count(*) FROM media_original_holds').fetchone()[0] == 0


async def test_audio_transcript_enters_final_batch_with_real_time_and_no_extra_model(native, tmp_path, monkeypatch):
    from companion_qq import native_documents

    _,anchor,_,downloads,_=await prepare(native,tmp_path,kind='audio')
    limits=[]
    def recognize(content,*,max_audio_ms,guard):
        guard()
        limits.append(max_audio_ms)
        return {'units':[{'source':{'start_ms':100,'end_ms':900},'text':'合成语音文字'}],
                'status':'ok','format':'speech','scope':'local_asr_transcript_may_contain_errors',
                'truncated':False,'actual_audio_ms':1000}
    monkeypatch.setattr(native_documents,'parse_audio',recognize)
    batch=native.module.decision_batch(anchor)
    assert (await native.core.handle(anchor))['action']['status']=='succeeded'
    material=[c['event_material'] for c in native.model.calls if 'event_material' in c]
    assert len(material)==1 and len(downloads)==1 and limits==[120000]
    assert material[0]['attachment_text']['items'][0]['units'][0]['text']=='合成语音文字'
    coverage=native.store.state(batch['key'])['coverage']
    assert coverage['media_budget']['audio_ms']==1000
    assert coverage['attachment_text']['items'][0]['units'][0]['source']=={'start_ms':100,'end_ms':900}


async def test_over_duration_audio_is_explicit_unread_and_keeps_text(native,tmp_path,monkeypatch):
    from companion_qq import native_documents

    _,anchor,_,_,_=await prepare(native,tmp_path,kind='audio')
    def too_long(*args,**kwargs):
        raise AssistantError('audio_duration','limit')
    monkeypatch.setattr(native_documents,'parse_audio',too_long)
    async with native.module.decision_material(anchor,guard=native.core.guard) as material:
        budget=material['context']['media_budget']
        assert budget['read']==0 and budget['unread']==1 and budget['audio_ms']==0
        assert budget['audio_budget_ms']==120000
        assert budget['reasons'][0]['code']=='audio_duration'
