"""This machine's own retained copies through the private portable channel.

The fixtures are the disposable desktop installation used by the other desktop suites, so
these cases exercise the real management session, the real preview slot and the real
preservation write on disk; the staging itself is driven through an injected recorder.
"""
import json
import secrets

import pytest

from companion_core import postgres_transfer
from companion_core.errors import AssistantError
from qiban.desktop_identity import OWNER
from qiban.desktop_portable import PRESERVATION, PRESERVED_EFFECT, DesktopPortable
from tests_postgres.test_desktop_management import desktop_store, service

__all__ = ["desktop_store", "service"]

INSTALLATION = 'a' * 32
GENERATION = 'b' * 32
OTHER = 'c' * 32
PASSWORD = 'preserved-strong-password'
SUCCESS = {"switched": True, "restart_required": True, "restored": True}


class Stager:
    """Records the injected staging request; never stages anything itself."""

    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result, error

    def __call__(self, **arguments):
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return dict(self.result or SUCCESS)


@pytest.fixture
def root(tmp_path, service):
    """The installation root side of the provider: the original generation's store."""
    base = tmp_path / 'installation'
    (base / 'store' / 'history' / PRESERVATION).mkdir(parents=True, exist_ok=True)
    return base


def copy(service, folder, name=None):
    """One authentic retained copy, written exactly the way a preview writes one."""
    store = service.store
    blob = postgres_transfer.create(store, DesktopPortable._backup_key(store._test_key))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name or secrets.token_hex(16) + '.ccbackup')
    target.write_bytes(blob)
    return target


def preserved_folder(root):
    return root / 'store' / 'history' / PRESERVATION


def portable(service, root, *, stager=None, clock=None, keys=None, page=None):
    from portable_preservation import preserved_provider

    slots = {INSTALLATION: service.store._test_key, GENERATION: service.store._test_key,
             OTHER: service.store._test_key}
    if keys is not None:
        slots = keys
    options = {'page': page} if page is not None else {}
    arguments = {"preserved": preserved_provider(root, INSTALLATION, slots.get, **options)}
    if clock is not None:
        arguments["clock"] = clock
    return DesktopPortable(service, lambda: service.store._test_key, stager or Stager(), **arguments)


def handed(portable_class, _root, count=1):
    """Handles for the first `count` listed copies, newest first."""
    items, after = [], ''
    while len(items) < count:
        page = portable_class.preserved_list(after)
        items.extend(page['items'])
        after = page['next']
        if len(items) >= count:
            break
        assert after, 'listing ended before the expected number of copies'
    return [item['handle'] for item in items[:count]]


def test_listing_is_metadata_only_and_pages_without_exposing_an_address(service, root):
    folder = preserved_folder(root)
    for _ in range(3):
        copy(service, folder)
    portable_class = portable(service, root, page=2)
    first = portable_class.preserved_list('')
    assert len(first['items']) == 2 and first['next']
    assert set(first) == {'items', 'next'}
    for item in first['items']:
        assert set(item) == {'handle', 'name', 'taken_at', 'bytes', 'usable'}
        assert item['usable'] is True and item['name'].endswith('.ccbackup')
        assert item['taken_at'] and item['bytes'] > 0
    second = portable_class.preserved_list(first['next'])
    assert len(second['items']) == 1 and second['next'] == ''
    names = [item['name'] for item in first['items'] + second['items']]
    assert len(set(names)) == 3
    serialized = json.dumps([first, second], ensure_ascii=False)
    assert str(root) not in serialized and PRESERVATION not in serialized
    assert service.store._test_key.hex() not in serialized


def test_dispatch_accepts_only_the_list_shape_and_the_handle(service, root):
    copy(service, preserved_folder(root))
    portable_class = portable(service, root)
    answer = portable_class.dispatch({"action": "desktop-portable", "operation": "preserved-list",
                                      "arguments": {"after": ""}})
    assert answer['ok'] is True and len(answer['result']['items']) == 1
    handle = answer['result']['items'][0]['handle']
    accepted = portable_class.dispatch({"action": "desktop-portable", "operation": "preserved-preview",
                                        "arguments": {"handle": handle}})
    assert accepted['ok'] is True and accepted['result']['kind'] == 'preserved'
    for arguments in ({"after": "", "password": PASSWORD},
                      {"after": "", "path": r"C:\synthetic\a.ccbackup"},
                      {"handle": handle, "password": PASSWORD},
                      {"handle": handle, "keyref": GENERATION},
                      {}):
        operation = 'preserved-preview' if 'handle' in arguments or 'keyref' in arguments else 'preserved-list'
        refused = portable_class.dispatch({"action": "desktop-portable", "operation": operation,
                                           "arguments": arguments})
        assert refused['ok'] is False and refused['error'] == 'management_request'
    assert portable_class.dispatch({"action": "desktop-portable", "operation": "preserved-restore",
                                    "arguments": {}})['error'] == 'management_request'


def test_preview_authenticates_one_copy_and_preserves_the_current_data(service, root):
    folder = preserved_folder(root)
    copy(service, folder)
    portable_class = portable(service, root)
    fingerprint = postgres_transfer.fingerprint(service.store)
    # The copy of the *current* data is written into the current store's own folder, which is
    # the store root of this fixture, not the enumerated installation root.
    store_folder = service.store.root / 'history' / PRESERVATION
    before = set(store_folder.glob('*.ccbackup')) if store_folder.is_dir() else set()
    result = portable_class.preserved_preview(handed(portable_class, root)[0])
    assert result['kind'] == 'preserved' and result['token']
    assert result['expires_in_seconds'] == 600
    assert result['source']['role'] == service.store.read('persona')['data']['name']
    assert result['records'] > 0 and isinstance(result['history'], int)
    assert result['name'].endswith('.ccbackup') and result['taken_at']
    assert '完整替换' in result['effect'] and result['effect'] == PRESERVED_EFFECT
    assert result['preservation']['name'].endswith('.ccbackup')
    assert 'counters' not in result and 'backup' not in result and 'key_file' not in result
    # Current data is untouched and was retained first, exactly as a portable preview does.
    assert postgres_transfer.fingerprint(service.store) == fingerprint
    created = set(store_folder.glob('*.ccbackup')) - before
    assert len(created) == 1
    assert portable_class.preview_state['mode'] == 'preserved'
    assert portable_class.preview_state['keyref'] == INSTALLATION


def test_preview_refuses_a_damaged_or_foreign_copy(service, root):
    folder = preserved_folder(root)
    damaged = preserved_folder(root) / 'damaged.ccbackup'
    damaged.write_bytes(b'CCBACKUP1:' + b'0' * 32 + b'\nnot-a-snapshot')
    portable_class = portable(service, root)
    with pytest.raises(AssistantError) as refused:
        portable_class.preserved_preview(handed(portable_class, root)[0])
    assert refused.value.code == 'backup_invalid'
    # A copy of this machine's own data key carrying another installation's physical owner
    # could only have been planted here; it must never replace this installation's identity.
    current = service.store.state(OWNER)
    try:
        service.store.set_state(OWNER, {**current, 'installation': OTHER})
        copy(service, folder, 'foreign.ccbackup')
    finally:
        service.store.set_state(OWNER, current)
    portable_class = portable(service, root, keys={INSTALLATION: service.store._test_key})
    names = [item['name'] for item in portable_class.preserved_list('')['items']]
    handle = next(item['handle'] for item in portable_class.preserved_list('')['items']
                  if item['name'] == 'foreign.ccbackup')
    assert 'foreign.ccbackup' in names
    with pytest.raises(AssistantError) as planted:
        portable_class.preserved_preview(handle)
    assert planted.value.code == 'preserved_source'
    assert portable_class.source_key is None and portable_class.preview_state is None


def test_a_preview_and_a_challenge_expire_and_a_handle_is_used_once(service, root):
    copy(service, preserved_folder(root))
    now = [1000.0]
    portable_class = portable(service, root, clock=lambda: now[0])
    first = portable_class.preserved_preview(handed(portable_class, root)[0])
    now[0] += 601
    with pytest.raises(AssistantError) as expired:
        portable_class.begin(first['token'])
    assert expired.value.code == 'portable_preview_expired'
    second = portable_class.preserved_preview(handed(portable_class, root)[0])
    challenge = portable_class.begin(second['token'])['challenge']
    with pytest.raises(AssistantError) as reused:
        portable_class.begin(second['token'])
    assert reused.value.code == 'portable_preview_expired'
    now[0] += 181
    with pytest.raises(AssistantError) as late:
        portable_class.complete(challenge)
    assert late.value.code == 'portable_verification_expired'
    assert portable_class.source_key is None and portable_class.pending is None


def test_completion_stages_the_preserved_mode_and_reports_an_unknown_outcome(service, root):
    copy(service, preserved_folder(root))
    stager = Stager()
    portable_class = portable(service, root, stager=stager)
    preview = portable_class.preserved_preview(handed(portable_class, root)[0])
    challenge = portable_class.begin(preview['token'])['challenge']
    result = portable_class.complete(challenge)
    assert result['switched'] is True and result['restart_required'] is True
    assert len(stager.calls) == 1 and stager.calls[0]['source_mode'] == 'preserved'
    assert set(stager.calls[0]) == {'source_blob', 'source_key', 'expected_current_fingerprint',
                                    'preservation', 'source_mode', 'source_name'}
    # The hand-off names the retained copy the preview authenticated, and the copy preserved
    # for this switch is a different file: the two are never the same name.
    assert stager.calls[0]['source_name'] == preview['name']
    assert stager.calls[0]['preservation']['name'] != preview['name']
    assert stager.calls[0]['preservation']['name'].endswith('.ccbackup')
    assert portable_class.source_key is None and portable_class.preview_state is None
    # A stager that fails after the hand-off is an unknown outcome, never a retry.
    failing = portable(service, root, stager=Stager(error=RuntimeError('synthetic interruption')))
    preview = failing.preserved_preview(handed(failing, root)[0])
    challenge = failing.begin(preview['token'])['challenge']
    with pytest.raises(AssistantError) as uncertain:
        failing.complete(challenge)
    assert uncertain.value.code == 'portable_switch_uncertain'


def test_a_portable_pair_keeps_its_existing_default_mode(service, root, tmp_path):
    target = tmp_path / 'kit.ccbackup'
    exporter = portable(service, root)
    exported = exporter.export(str(target), PASSWORD)
    stager = Stager()
    portable_class = portable(service, root, stager=stager)
    preview = portable_class.preview(str(target), str(tmp_path / exported['key_file']), PASSWORD)
    challenge = portable_class.begin(preview['token'])['challenge']
    portable_class.complete(challenge)
    assert 'source_mode' not in stager.calls[0]
    # A portable import under the default mode still names its own backup file as the source.
    assert stager.calls[0]['source_name'] == 'kit.ccbackup'
    assert stager.calls[0]['preservation']['name'] != 'kit.ccbackup'
    assert preview['kind'] == 'portable' and 'taken_at' not in preview


def test_a_copy_that_changed_after_the_preview_is_refused(service, root):
    folder = preserved_folder(root)
    target = copy(service, folder)
    stager = Stager()
    portable_class = portable(service, root, stager=stager)
    preview = portable_class.preserved_preview(handed(portable_class, root)[0])
    challenge = portable_class.begin(preview['token'])['challenge']
    original = target.read_bytes()
    target.write_bytes(original + b'\0')
    with pytest.raises(AssistantError) as changed:
        portable_class.complete(challenge)
    assert changed.value.code == 'preserved_changed'
    assert stager.calls == []


def test_edits_to_the_current_data_after_the_preview_are_refused(service, root):
    copy(service, preserved_folder(root))
    stager = Stager()
    portable_class = portable(service, root, stager=stager)
    preview = portable_class.preserved_preview(handed(portable_class, root)[0])
    challenge = portable_class.begin(preview['token'])['challenge']
    service.store.set_state('desktop-notice:after-preserved-preview', {'code': 'synthetic'})
    with pytest.raises(AssistantError) as refused:
        portable_class.complete(challenge)
    assert refused.value.code == 'portable_changed'
    assert stager.calls == []


def test_locking_drops_the_armed_preview_and_its_source_key(service, root):
    copy(service, preserved_folder(root))
    stager = Stager()
    portable_class = portable(service, root, stager=stager)
    preview = portable_class.preserved_preview(handed(portable_class, root)[0])
    challenge = portable_class.begin(preview['token'])['challenge']
    service.unlocked = False
    try:
        with pytest.raises(AssistantError) as locked:
            portable_class.complete(challenge)
    finally:
        service.unlocked = True
    assert locked.value.code == 'desktop_locked'
    assert portable_class.source_key is None and portable_class.pending is None
    assert stager.calls == []
