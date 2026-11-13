import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from filelock import FileLock

# The probe scripts are executed from their directory, not installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'desktop_probe'))

from installation_generation import (  # noqa: E402
    commit_selection,
    load_selection,
    prepare_selection,
)

from companion_core.errors import AssistantError  # noqa: E402

INSTALLATION = '0123456789abcdef0123456789abcdef'
GENERATION = 'fedcba9876543210fedcba9876543210'
OTHER = 'a' * 32
ORIGINAL = {'installation': INSTALLATION, 'generation': None}


@pytest.fixture
def root(tmp_path):
    """An installed root whose original generation still owns <root>/store."""
    installation = tmp_path / 'installation'
    (installation / 'store').mkdir(parents=True)
    (installation / 'database').mkdir()
    return installation.resolve()


def prepared(root, generation=GENERATION):
    """Layout a restored generation whose data directory already exists, then register it."""
    directory = root / 'generations' / generation
    (directory / 'store').mkdir(parents=True)
    prepare_selection(root, INSTALLATION, generation)
    return directory


def pointer(root):
    return json.loads((root / 'active-generation.json').read_text(encoding='utf8'))


def link(source, target):
    """A directory reparse point, without needing Windows symlink privilege."""
    try:
        os.symlink(source, target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != 'nt':
            pytest.skip('directory links are unavailable on this host')
    from _winapi import CreateJunction

    CreateJunction(str(source), str(target))


def test_absent_pointer_selects_the_original_generation(root):
    selection = load_selection(root, INSTALLATION)
    assert selection['generation'] is None
    assert selection['database'] == 'qiban'
    # The original generation's credential is the installation's own keyring entry, so its id
    # is the installation identifier itself, not a literal.
    assert selection['credential_id'] == INSTALLATION
    assert selection['credential_id'] != 'installation'
    assert load_selection(root, OTHER)['credential_id'] == OTHER
    assert Path(selection['store']) == root / 'store'
    assert not (root / 'active-generation.json').exists()


def test_missing_original_store_is_not_selected_as_an_empty_generation(root):
    (root / 'store').rmdir()
    with pytest.raises(AssistantError) as error:
        load_selection(root, INSTALLATION)
    assert error.value.code == 'generation_store_missing'


def test_preparing_a_generation_does_not_switch_the_selection(root):
    directory = prepared(root)
    ready = json.loads((directory / 'generation.json').read_text(encoding='utf8'))
    assert ready == {'format': 1, 'installation': INSTALLATION, 'id': GENERATION, 'ready': True}
    assert load_selection(root, INSTALLATION)['generation'] is None
    assert not (root / 'active-generation.json').exists()
    # Registering the identical manifest again is a no-op, never a rewrite.
    registered = (directory / 'generation.json').read_bytes()
    again = prepare_selection(root, INSTALLATION, GENERATION)
    assert again['generation'] == GENERATION
    assert (directory / 'generation.json').read_bytes() == registered


def test_commit_selects_the_prepared_generation(root):
    directory = prepared(root)
    selection = commit_selection(root, INSTALLATION, GENERATION, load_selection(root, INSTALLATION))
    assert selection['generation'] == GENERATION
    assert selection['database'] == 'qiban_restore_' + GENERATION
    assert selection['credential_id'] == GENERATION
    assert Path(selection['store']) == directory / 'store'
    digest = hashlib.sha256((directory / 'generation.json').read_bytes()).hexdigest()
    assert pointer(root) == {'format': 1, 'installation': INSTALLATION, 'generation': GENERATION,
                             'manifest_sha256': digest}
    assert load_selection(root, INSTALLATION) == selection


def test_lost_response_replay_returns_the_same_selection_without_writing(root, monkeypatch):
    prepared(root)
    committed = commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    pointer_before = (root / 'active-generation.json').read_bytes()

    def refused(*_):
        raise AssertionError('a replay of the completed commit must not write again')

    monkeypatch.setattr('installation_generation._replace', refused)
    # The caller never saw the first answer and retries with the expectation it still holds.
    assert commit_selection(root, INSTALLATION, GENERATION, ORIGINAL) == committed
    assert commit_selection(root, INSTALLATION, GENERATION, committed) == committed
    assert (root / 'active-generation.json').read_bytes() == pointer_before


def test_stale_expectation_is_refused(root):
    prepared(root, GENERATION)
    prepared(root, OTHER)
    with pytest.raises(AssistantError) as stale:
        commit_selection(root, INSTALLATION, GENERATION, {'installation': INSTALLATION, 'generation': OTHER})
    assert stale.value.code == 'generation_selection_stale'
    with pytest.raises(AssistantError) as incomplete:
        commit_selection(root, INSTALLATION, GENERATION, {'installation': INSTALLATION})
    assert incomplete.value.code == 'generation_expected_invalid'
    with pytest.raises(AssistantError) as malformed:
        commit_selection(root, INSTALLATION, GENERATION, {'installation': INSTALLATION, 'generation': '..'})
    assert malformed.value.code == 'generation_expected_invalid'
    with pytest.raises(AssistantError) as foreign:
        commit_selection(root, INSTALLATION, GENERATION, {'installation': OTHER, 'generation': None})
    assert foreign.value.code == 'generation_installation_mismatch'
    assert not (root / 'active-generation.json').exists()
    commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    # A worker still holding the original expectation cannot pick another target afterwards.
    with pytest.raises(AssistantError) as moved:
        commit_selection(root, INSTALLATION, OTHER, ORIGINAL)
    assert moved.value.code == 'generation_selection_stale'
    assert pointer(root)['generation'] == GENERATION


def test_a_held_commit_lock_is_an_explicit_busy_error(root):
    prepared(root)
    holder = FileLock(str(root / 'generation.lock'), timeout=0)
    holder.acquire()
    try:
        with pytest.raises(AssistantError) as error:
            commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    finally:
        holder.release()
    assert error.value.code == 'generation_busy'
    assert not (root / 'active-generation.json').exists()


def test_two_workers_sharing_one_expectation_commit_exactly_once(root, monkeypatch):
    prepared(root, GENERATION)
    prepared(root, OTHER)
    # A held lock is refused at once; a bounded wait lets the loser of the race observe the
    # winner's committed selection, which is the refusal under test here.
    monkeypatch.setattr('installation_generation.LOCK_TIMEOUT', 5)
    start = threading.Barrier(2)
    results = {}

    def worker(name, target):
        start.wait(5)
        try:
            results[name] = commit_selection(root, INSTALLATION, target, ORIGINAL)
        except BaseException as error:  # the outcome itself is the assertion
            results[name] = error

    threads = [threading.Thread(target=worker, args=('a', GENERATION)),
               threading.Thread(target=worker, args=('b', OTHER))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads)
    committed = [value for value in results.values() if isinstance(value, dict)]
    refused = [value for value in results.values() if isinstance(value, BaseException)]
    assert len(committed) == 1
    assert len(refused) == 1
    assert isinstance(refused[0], AssistantError) and refused[0].code == 'generation_selection_stale'
    assert pointer(root)['generation'] == committed[0]['generation']
    assert load_selection(root, INSTALLATION) == committed[0]


def test_corrupt_pointer_is_refused_instead_of_falling_back(root):
    prepared(root)
    commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    path = root / 'active-generation.json'
    path.write_text('{', encoding='utf8')
    with pytest.raises(AssistantError) as unreadable:
        load_selection(root, INSTALLATION)
    assert unreadable.value.code == 'generation_pointer_invalid'
    path.write_text(json.dumps({'format': 1, 'installation': INSTALLATION}), encoding='utf8')
    with pytest.raises(AssistantError) as truncated:
        load_selection(root, INSTALLATION)
    assert truncated.value.code == 'generation_pointer_invalid'
    path.write_text(json.dumps({'format': 1, 'installation': OTHER, 'generation': GENERATION,
                                'manifest_sha256': '0' * 64}), encoding='utf8')
    with pytest.raises(AssistantError) as foreign:
        load_selection(root, INSTALLATION)
    assert foreign.value.code == 'generation_installation_mismatch'
    assert pointer(root)['generation'] == GENERATION


def test_tampered_or_missing_manifest_is_refused(root):
    directory = prepared(root)
    commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    manifest = directory / 'generation.json'
    # A path field cannot be smuggled in, even when the pointer digest is refreshed to match.
    manifest.write_text(json.dumps({'format': 1, 'installation': INSTALLATION, 'id': GENERATION,
                                    'ready': True, 'store': 'C:\\elsewhere'}), encoding='utf8')
    refreshed = pointer(root)
    refreshed['manifest_sha256'] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (root / 'active-generation.json').write_text(json.dumps(refreshed), encoding='utf8')
    with pytest.raises(AssistantError) as forged:
        load_selection(root, INSTALLATION)
    assert forged.value.code == 'generation_manifest_invalid'
    # The same fields with different bytes no longer match the committed digest.
    manifest.write_text(json.dumps({'format': 1, 'installation': INSTALLATION, 'id': GENERATION,
                                    'ready': True}, indent=2), encoding='utf8')
    with pytest.raises(AssistantError) as tampered:
        load_selection(root, INSTALLATION)
    assert tampered.value.code == 'generation_manifest_hash_mismatch'
    manifest.unlink()
    with pytest.raises(AssistantError) as missing:
        load_selection(root, INSTALLATION)
    assert missing.value.code == 'generation_manifest_missing'
    assert pointer(root)['generation'] == GENERATION


def test_unready_or_foreign_manifest_is_never_committed(root):
    unready = root / 'generations' / GENERATION / 'store'
    unready.mkdir(parents=True)
    (unready.parent / 'generation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'id': GENERATION, 'ready': False}), encoding='utf8')
    with pytest.raises(AssistantError) as error:
        commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    assert error.value.code == 'generation_manifest_invalid'
    foreign = root / 'generations' / OTHER / 'store'
    foreign.mkdir(parents=True)
    (foreign.parent / 'generation.json').write_text(json.dumps(
        {'format': 1, 'installation': 'b' * 32, 'id': OTHER, 'ready': True}), encoding='utf8')
    with pytest.raises(AssistantError) as mismatch:
        commit_selection(root, INSTALLATION, OTHER, ORIGINAL)
    assert mismatch.value.code == 'generation_installation_mismatch'
    assert not (root / 'active-generation.json').exists()


def test_interruption_before_the_replacement_keeps_the_original_generation(root, monkeypatch):
    prepared(root)

    def interrupted(temporary, path):
        raise RuntimeError('interrupted before replacement')

    monkeypatch.setattr('installation_generation._replace', interrupted)
    with pytest.raises(RuntimeError):
        commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    assert load_selection(root, INSTALLATION)['generation'] is None
    assert not (root / 'active-generation.json').exists()
    # The interrupted attempt released the commit lock, so a later commit still proceeds.
    monkeypatch.undo()
    assert commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)['generation'] == GENERATION


def test_interruption_after_the_replacement_keeps_the_new_generation(root, monkeypatch):
    prepared(root)
    replaced = os.replace

    def interrupted(temporary, path):
        replaced(temporary, path)
        raise RuntimeError('interrupted after replacement')

    monkeypatch.setattr('installation_generation._replace', interrupted)
    with pytest.raises(RuntimeError):
        commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    assert load_selection(root, INSTALLATION)['generation'] == GENERATION


def test_committing_creates_only_bookkeeping_files(root):
    (root / 'store' / 'original-record').write_text('original', encoding='utf8')
    directory = prepared(root)
    before = {path.relative_to(root).as_posix() for path in root.rglob('*')}
    commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    after = {path.relative_to(root).as_posix() for path in root.rglob('*')}
    # The commit lock is bookkeeping and may or may not be left behind by the platform.
    assert after - before <= {'active-generation.json', 'generation.lock'}
    assert not before - after
    assert 'active-generation.json' in after
    assert (root / 'store' / 'original-record').read_text(encoding='utf8') == 'original'
    assert (directory / 'generation.json').is_file()
    assert (directory / 'store').is_dir()
    assert (root / 'database').is_dir()


@pytest.mark.parametrize('generation', [
    '', '..', '../store', 'C:\\Temp', GENERATION.upper(), 'f' * 31, 'g' * 32])
def test_generation_ids_must_be_a_plain_identifier(root, generation):
    with pytest.raises(AssistantError) as error:
        prepare_selection(root, INSTALLATION, generation)
    assert error.value.code == 'generation_id_invalid'
    with pytest.raises(AssistantError) as committed:
        commit_selection(root, INSTALLATION, generation, ORIGINAL)
    assert committed.value.code == 'generation_id_invalid'
    assert not (root / 'generations').exists()


def test_reparse_points_inside_the_root_are_refused(root):
    directory = root / 'generations' / GENERATION
    directory.parent.mkdir()
    # A link that stays inside the root is still refused instead of silently followed.
    link(root / 'store', directory)
    with pytest.raises(AssistantError) as linked:
        prepare_selection(root, INSTALLATION, GENERATION)
    assert linked.value.code == 'generation_path'
    outside = root.parent / 'outside'
    outside.mkdir()
    link(outside, root / 'active-generation.json')
    with pytest.raises(AssistantError) as escaped:
        load_selection(root, INSTALLATION)
    assert escaped.value.code == 'generation_path'
