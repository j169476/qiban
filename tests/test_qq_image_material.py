from types import SimpleNamespace

import pytest

from companion_core.media_budget import DEFAULTS
from companion_qq.native_documents import prepare_documents
from tests.test_image_reader import animation


@pytest.mark.parametrize('slots,expected,downloads', [(1, [[0]], 1), (2, [[0, 4]], 1), (3, [[0, 2, 4]], 1), (4, [[0, 2, 4], [0]], 2)])
def test_frames_share_turn_budget_and_skip_download_when_exhausted(monkeypatch, slots, expected, downloads):
    from companion_qq import native_documents as module

    candidates = [{'id': str(i), 'event_id': str(i), 'source_version': 'v', 'kind': 'image'} for i in range(2)]
    received = []
    class Originals:
        def __init__(self, store):
            pass
        def read(self, *args, guard):
            guard()
            return animation()
    class Permissions:
        def __init__(self, store):
            pass
        def version(self, scope):
            return 'p'
        def check(self, *args):
            pass
    def save(env, candidate, **kwargs):
        received.append(candidate['id'])
        return {'name': 'animation.gif'}
    monkeypatch.setattr(module, 'MediaOriginals', Originals)
    monkeypatch.setattr(module, 'ReplyAuthorizations', Permissions)
    monkeypatch.setattr(module.events, 'get', lambda store, key: {'version': 'v', 'event': {'id': key}})
    monkeypatch.setattr(module, 'Event', SimpleNamespace(from_dict=lambda row: SimpleNamespace(**row)))
    env = SimpleNamespace(store=None, scope=lambda event: None, validate_context_event=lambda event: None,
                          provider=SimpleNamespace(save_attachment=save))
    result = prepare_documents(env, {'limits': {**DEFAULTS, 'images': slots}, 'candidates': candidates,
        'reasons': [], 'unread': 2}, owner='o', task='t', guard=lambda: None)
    assert [[f['index'] for f in item['frames']] for item in result['image_sources']] == expected
    assert len(received) == downloads
    assert result['images_read'] == sum(map(len, expected))
    assert result['media_budget']['unread'] == 2-downloads
    if downloads == 1:
        assert result['media_budget']['reasons'][0]['code'] == 'image_frame_budget'
