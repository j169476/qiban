"""The retained pre-replacement copies of one installation, without a database.

Every case here builds a synthetic installation root: the original generation's store, a
ready generation's store and the preservation folder ``history/portable-preservation``.
The provider is trusted host code, so these checks cover what it may derive, page, hand out
and refuse; the payload authentication itself is covered by the service and staging suites.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'desktop_probe'))

import portable_preservation  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402
from qiban.desktop_portable import PRESERVATION  # noqa: E402

INSTALLATION = 'a' * 32
GENERATION = 'b' * 32
KEY = bytes(range(32))
PAYLOAD = b'CCBACKUP1:' + b'0' * 32 + b'\nsynthetic-preserved-payload'


def installation(tmp_path):
    base = tmp_path / 'installation'
    (base / 'store').mkdir(parents=True)
    return base


def copied(base, folder, name, payload=PAYLOAD):
    target = base / folder / 'history' / PRESERVATION / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def generation(base, name, *, ready=True, identifier=INSTALLATION):
    directory = base / 'generations' / name
    (directory / 'store').mkdir(parents=True)
    (directory / 'generation.json').write_text(json.dumps(
        {'format': 1, 'installation': identifier, 'id': name, 'ready': ready}), encoding='utf8')
    return directory


def provider(base, *, page=portable_preservation.PAGE, keys=None):
    slots = {INSTALLATION: KEY, GENERATION: KEY} if keys is None else keys
    return portable_preservation.preserved_provider(base, INSTALLATION, slots.get, page=page)


def everything(base, **kwargs):
    """Every page of one listing, so a paginated case proves nothing is dropped."""
    instance, items, after = provider(base, **kwargs), [], ''
    for _ in range(20):
        page = instance.page(after)
        items.extend(page['items'])
        after = page['next']
        if not after:
            break
    return instance, items


def test_listing_pages_ordinary_file_metadata_across_original_and_ready_generations(tmp_path):
    base = installation(tmp_path)
    generation(base, GENERATION)
    for index, (folder, name) in enumerate((
            ('store', 'aaaa.ccbackup'), ('store', 'bbbb.ccbackup'),
            ('generations/' + GENERATION + '/store', 'cccc.ccbackup'))):
        copied(base, folder, name)
        os.utime(base / folder / 'history' / PRESERVATION / name, (1_700_000_000 + index,) * 2)
    instance, items = everything(base, page=2)
    assert len(items) == 3
    # Newest file time first, original and restored generation alike.
    assert [item['name'] for item in items] == ['cccc.ccbackup', 'bbbb.ccbackup', 'aaaa.ccbackup']
    for item in items:
        assert set(item) == {'handle', 'name', 'taken_at', 'bytes', 'usable'}
        assert item['usable'] is True and item['bytes'] == len(PAYLOAD)
        assert item['taken_at'].startswith('2023-11-')
        assert item['handle'] and item['handle'] != item['name']
    # The cursor names a position, never a path or a generation.
    first = instance.page('')
    assert len(first['items']) == 2 and first['next']
    assert set(first) == {'items', 'next'}
    second = instance.page(first['next'])
    assert [item['name'] for item in second['items']] == ['aaaa.ccbackup'] and second['next'] == ''


def test_a_handle_resolves_only_through_the_derived_address(tmp_path):
    base = installation(tmp_path)
    generation(base, GENERATION)
    copied(base, 'generations/' + GENERATION + '/store', 'preserved.ccbackup')
    instance = provider(base)
    handle = instance.page('')['items'][0]['handle']
    opened = instance.open(handle)
    assert opened['blob'] == PAYLOAD and opened['key'] == KEY
    assert opened['keyref'] == GENERATION and opened['name'] == 'preserved.ccbackup'
    assert instance.reopen(GENERATION, 'preserved.ccbackup')['blob'] == PAYLOAD
    for bad in ('', 'not-a-handle', handle + 'x'):
        with pytest.raises(AssistantError) as refused:
            instance.open(bad)
        assert refused.value.code == 'preserved_handle'
    with pytest.raises(AssistantError) as missing:
        instance.reopen(GENERATION, 'absent.ccbackup')
    assert missing.value.code == 'preserved_missing'
    for name in ('/etc/passwd.ccbackup', '..\\x.ccbackup', 'sub/dir.ccbackup', 'plain.txt'):
        with pytest.raises(AssistantError) as refused:
            instance.reopen(GENERATION, name)
        assert refused.value.code == 'preserved_handle'
    with pytest.raises(AssistantError) as unknown:
        instance.reopen('c' * 32, 'preserved.ccbackup')
    assert unknown.value.code == 'preserved_handle'
    with pytest.raises(AssistantError) as cursor:
        instance.page('unknown-cursor')
    assert cursor.value.code == 'preserved_handle'


def test_unusable_candidates_stay_visible_and_are_never_read(tmp_path):
    base = installation(tmp_path)
    folder = base / 'store' / 'history' / PRESERVATION
    folder.mkdir(parents=True)
    (folder / 'empty.ccbackup').write_bytes(b'')
    (folder / 'directory.ccbackup').mkdir()
    copied(base, 'store', 'good.ccbackup')
    instance, items = everything(base)
    assert {item['name']: item['usable'] for item in items} == {
        'empty.ccbackup': False, 'directory.ccbackup': False, 'good.ccbackup': True}
    unusable = next(item for item in items if item['name'] == 'empty.ccbackup')
    with pytest.raises(AssistantError) as refused:
        instance.open(unusable['handle'])
    assert refused.value.code == 'preserved_changed'


def test_a_linked_candidate_is_listed_unusable_and_never_followed(tmp_path):
    base = installation(tmp_path)
    copied(base, 'store', 'real.ccbackup')
    outside = tmp_path / 'outside.ccbackup'
    outside.write_bytes(PAYLOAD)
    try:
        os.symlink(outside, base / 'store' / 'history' / PRESERVATION / 'linked.ccbackup')
    except (OSError, NotImplementedError):
        pytest.skip('This host cannot create a symbolic link for the link case')
    instance, items = everything(base)
    linked = next(item for item in items if item['name'] == 'linked.ccbackup')
    assert linked['usable'] is False
    with pytest.raises(AssistantError):
        instance.open(linked['handle'])


def test_only_ready_generations_of_this_installation_are_enumerated(tmp_path):
    base = installation(tmp_path)
    generation(base, GENERATION)
    generation(base, 'c' * 32, ready=False)
    generation(base, 'd' * 32, identifier='e' * 32)
    generation(base, 'f' * 32)
    (base / 'generations' / 'not-an-identifier').mkdir()
    copied(base, 'generations/' + GENERATION + '/store', 'ready.ccbackup')
    copied(base, 'generations/' + 'c' * 32 + '/store', 'unready.ccbackup')
    copied(base, 'generations/' + 'd' * 32 + '/store', 'foreign.ccbackup')
    copied(base, 'generations/' + 'f' * 32 + '/store', 'ready2.ccbackup')
    copied(base, 'generations/not-an-identifier/store', 'ignored.ccbackup')
    _, items = everything(base)
    assert sorted(item['name'] for item in items) == ['ready.ccbackup', 'ready2.ccbackup']


def test_two_copies_with_the_same_file_name_and_time_are_both_paged_and_read_with_their_own_key(tmp_path):
    base = installation(tmp_path)
    generation(base, GENERATION)
    original = copied(base, 'store', 'shared.ccbackup', b'synthetic-original-body')
    bridged = copied(base, 'generations/' + GENERATION + '/store', 'shared.ccbackup',
                     b'synthetic-generation-body')
    stamp = 1_700_000_500
    os.utime(original, (stamp, stamp))
    os.utime(bridged, (stamp, stamp))
    slots = {INSTALLATION: bytes(32), GENERATION: bytes([7]) * 32}
    instance = portable_preservation.preserved_provider(base, INSTALLATION, slots.get, page=1)
    seen, after = [], ''
    for _ in range(4):
        page = instance.page(after)
        seen.extend(page['items'])
        after = page['next']
        if not after:
            break
    # Both copies share their name and file time; the unique order still pages both of them.
    assert [item['name'] for item in seen] == ['shared.ccbackup', 'shared.ccbackup']
    assert len({item['handle'] for item in seen}) == 2
    opened = {instance.open(item['handle'])['keyref']: instance.open(item['handle']) for item in seen}
    assert opened[INSTALLATION]['blob'] == b'synthetic-original-body'
    assert opened[GENERATION]['blob'] == b'synthetic-generation-body'
    assert opened[INSTALLATION]['key'] == slots[INSTALLATION]
    assert opened[GENERATION]['key'] == slots[GENERATION]


def test_a_missing_key_slot_or_store_fails_closed(tmp_path):
    base = installation(tmp_path)
    copied(base, 'store', 'preserved.ccbackup')
    instance = provider(base, keys={})
    with pytest.raises(AssistantError) as refused:
        instance.open(instance.page('')['items'][0]['handle'])
    assert refused.value.code == 'preserved_unavailable'
    with pytest.raises(AssistantError) as gone:
        portable_preservation.preserved_provider(
            tmp_path / 'absent', INSTALLATION, {INSTALLATION: KEY}.get).page('')
    assert gone.value.code == 'preserved_unavailable'


def test_a_replaced_or_deleted_copy_is_refused_before_it_is_read(tmp_path):
    base = installation(tmp_path)
    target = copied(base, 'store', 'preserved.ccbackup')
    instance = provider(base)
    handle = instance.page('')['items'][0]['handle']
    outside = tmp_path / 'outside.ccbackup'
    outside.write_bytes(PAYLOAD)
    try:
        target.unlink()
        os.symlink(outside, target)
        with pytest.raises(AssistantError) as changed:
            instance.open(handle)
        assert changed.value.code == 'preserved_changed'
        target.unlink()
    except (OSError, NotImplementedError):
        target.unlink(missing_ok=True)
    with pytest.raises(AssistantError) as missing:
        instance.open(handle)
    assert missing.value.code == 'preserved_missing'
    with pytest.raises(AssistantError) as gone:
        instance.reopen(INSTALLATION, 'preserved.ccbackup')
    assert gone.value.code == 'preserved_missing'
