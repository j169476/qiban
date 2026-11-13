"""Cross-schema rollback and its interruption behaviour, with strict store doubles.

The two halves of a rollback must never touch each other's schema: the new program opens the
clone with the new profile, the previous program opens only the recorded fallback with the old
profile. The store doubles here are strict - requesting the wrong (generation, profile) pair
raises - so an implementation that opened the clone from the old side, or the fallback from the
new side, fails instead of passing quietly.

The interruption cases are real: the first attempt writes part of the program directory and then
fails, and the retry finishes from the two manifests persisted in the record, not from whatever
the half-written directory happens to contain.
"""
import contextlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import upgrade.data as data  # noqa: E402
import upgrade.journal as journal  # noqa: E402
import upgrade.program as program  # noqa: E402
import upgrade.service as service  # noqa: E402
import upgrade.verification as verification  # noqa: E402
from test_upgrade_cleanup_gate import (  # noqa: E402
    INSTALLATION,
    OLD_GENERATION,
    artifact,
    generation_tree,
    record_at,
)
from test_upgrade_program import NEW_FILES, OLD_FILES, published  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402

TRANSACTION = 'e'*32
OLD_KEYS = {INSTALLATION: b'1'*32, OLD_GENERATION: b'3'*32}


class StrictStore:
    """A store that refuses any (generation, profile) pair it was not built for."""

    def __init__(self, generation, profile, allowed, log):
        if (generation, profile.tag) not in allowed:
            raise AssistantError('upgrade_database', f'cross-schema {generation}/{profile.tag}')
        self.generation, self.profile = generation, profile.tag
        log.append((generation, profile.tag))

    def db(self):
        return contextlib.nullcontext(object())

    class database:
        @staticmethod
        def close():
            return None


class Profile:
    def __init__(self, tag):
        self.tag = tag


@contextlib.contextmanager
def strict_session(log, allowed, generation, *, profile=None, selection=None, **_):
    chosen = generation if generation is not None else (
        selection['generation'] if selection is not None else None)
    yield StrictStore(chosen, profile, allowed, log), b'k'*32, {}, {}, selection


def prepared_phase(root, tmp_path, monkeypatch, *, phase='staged'):
    """A real record and real program directories in the requested phase."""
    old_dir, new_dir = tmp_path/'old', tmp_path/'new'
    old_dir.mkdir()
    new_dir.mkdir()
    previous = published(old_dir, OLD_FILES)
    installed = published(new_dir, NEW_FILES)
    generation_tree(root, OLD_GENERATION)
    registry = journal  # keep the import surface obvious
    record = journal.create(
        INSTALLATION,
        {'identity_sha256': previous['sha256'],
         'selection': {'generation': OLD_GENERATION,
                       'database': 'qiban_restore_'+OLD_GENERATION,
                       'store': 'generations/'+OLD_GENERATION+'/store',
                       'credential_id': OLD_GENERATION, 'manifest_sha256': '3'*64},
         'data': {'fingerprint': '1'*64, 'migrations_sha256': '2'*64}},
        {'identity_sha256': installed['sha256']},
        {'identity_sha256': previous['sha256'], 'count': previous['count'],
         'bytes': previous['bytes']},
        artifact(root), transaction=TRANSACTION)
    import upgrade.registry as registry_module

    registry_module.protect_directory = lambda path: None
    program.protect_directory = lambda path: None
    carrier = program.copy_carrier(root, old_dir, previous, installation=INSTALLATION,
                                   transaction=TRANSACTION, roles=['carrier'])
    record = journal.save(root, record)
    plan = data.plan_clone(INSTALLATION, TRANSACTION)
    record = journal.advance(root, record, 'staging', plan=plan)
    record = journal.advance(root, record, 'staged', clone={**plan, 'manifest_sha256': '5'*64})
    return {'record': record, 'previous': previous, 'installed': installed,
            'carrier': carrier, 'old_dir': old_dir, 'new_dir': new_dir, 'registry': registry}


def pointer(start):
    state = {'generation': start}

    def selected(root_value, installation):
        return {'generation': state['generation'],
                'database': 'qiban' if state['generation'] is None
                else 'qiban_restore_'+state['generation'],
                'store': 'store' if state['generation'] is None
                else 'generations/'+state['generation']+'/store',
                'credential_id': INSTALLATION if state['generation'] is None
                else state['generation'],
                'manifest_sha256': None if state['generation'] is None else '3'*64}

    def commit(root_value, installation, generation, current):
        assert current['generation'] == state['generation']
        state['generation'] = generation
        return selected(root_value, installation)

    return state, selected, commit


def wire(monkeypatch, world, *, log, allowed, profile_tag, store_opens=True):
    monkeypatch.setattr(service.locks, 'exclusive', lambda *a, **k: _nothing())
    monkeypatch.setattr(service, 'running', lambda: False)
    monkeypatch.setattr(service, 'desktop_profile', lambda: Profile(profile_tag))
    monkeypatch.setattr(service.data, 'slot_key', lambda vault, installation, keyref: b'k'*32)
    monkeypatch.setattr(service.data, 'local_settings',
                        lambda store, installation: {'desktop-owner': {'installation': INSTALLATION},
                                                     'desktop-lock': {'enabled': True,
                                                                      'revision': 3}})
    monkeypatch.setattr(service.data, 'preserve',
                        lambda store, key: {'name': 'cur.ccbackup', 'sha256': 'a'*64,
                                            'bytes': 128})
    monkeypatch.setattr(service.data, 'fingerprint', lambda store: 'b'*64)
    monkeypatch.setattr(service.data, 'migrations', lambda store: 'c'*64)
    applied = []
    monkeypatch.setattr(service.data, 'apply_recovery',
                        lambda store, settings, *, application_session, generation, db:
                        applied.append({'generation': generation, 'settings': settings,
                                        'session': application_session, 'profile': store.profile}))
    if store_opens:
        @contextlib.contextmanager
        def session(root_value, app, *, selection=None, profile=None, **kwargs):
            descriptor = selection if selection is not None else world['record']['clone']
            yield StrictStore(descriptor['generation'], profile, allowed,
                              log), b'k'*32, {}, {}, descriptor
        monkeypatch.setattr(service.data, 'session', session)
    return applied


@contextlib.contextmanager
def _nothing():
    yield


def install_channel(monkeypatch, *, fail=False):
    calls = []

    def handler(request):
        calls.append(request)
        if fail:
            raise AssistantError('upgrade_verification_invalid', 'cancelled')
        return {'session': 'windows-1', 'at': __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc).isoformat()}

    verification.install(handler)
    return calls


@pytest.fixture(autouse=True)
def reset_channel():
    verification.clear()
    yield
    verification.clear()


@pytest.fixture(autouse=True)
def acl(monkeypatch):
    """The ACL call needs Windows privileges the offline runner does not grant."""
    monkeypatch.setattr(journal, 'protect_directory', lambda path: None)
    monkeypatch.setattr(program, 'protect_directory', lambda path: None)


def test_the_rollback_halves_open_only_their_own_schema(tmp_path, monkeypatch):
    fixture_root = tmp_path/'Profile'
    (fixture_root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (fixture_root/'installation.json').write_text(
        '{"format": 1, "installation": "%s", "owner": "o", "port": 54329, "phase": "ready"}'
        % INSTALLATION, encoding='utf8')
    root = fixture_root
    world = prepared_phase(root, tmp_path, monkeypatch)
    state, selected, commit = pointer(world['record']['clone']['generation'])
    monkeypatch.setattr(service.data, 'selection_generation', lambda *a: state['generation'])
    monkeypatch.setattr(service.data, 'selected', selected)
    monkeypatch.setattr(service.data, 'commit_selection', commit)
    calls = install_channel(monkeypatch)
    log = []
    applied = wire(monkeypatch, world, log=log, allowed={(world['record']['clone']['generation'],
                                                          'new'),
                                                         (OLD_GENERATION, 'old')},
                   profile_tag='new')
    ready = service.rollback_prepare(root, app=world['new_dir'])
    assert ready['phase'] == 'rollback-ready'
    assert log == [(world['record']['clone']['generation'], 'new')], \
        'preparing a rollback reads the clone with the new profile only'
    sealed = journal.load(root)['rollback']['settings']
    assert sealed is not None and sealed['keyref'] == OLD_GENERATION
    assert 'desktop-owner' not in str(sealed), 'the whitelist is not written in clear text'

    log.clear()
    monkeypatch.setattr(service, 'desktop_profile', lambda: Profile('old'))
    monkeypatch.setattr(service, 'executing_app', lambda: world['carrier'])
    monkeypatch.setattr(service.data, 'selection_generation', lambda *a: state['generation'])
    monkeypatch.setattr(service.data, 'selected', selected)
    monkeypatch.setattr(service.data, 'commit_selection', commit)
    report = service.rollback(root, installed_app=world['new_dir'])
    assert report['phase'] == 'rolled-back'
    assert log == [(OLD_GENERATION, 'old')], \
        'the previous program opens the fallback generation with its own profile only'
    assert applied[-1]['generation'] == OLD_GENERATION
    assert applied[-1]['settings']['desktop-owner'] == {'installation': INSTALLATION}
    assert state['generation'] == OLD_GENERATION
    assert calls and calls[0]['action'] == 'rollback'
    for row in world['previous']['files']:
        import hashlib

        assert hashlib.sha256((world['new_dir']/row['path']).read_bytes()).hexdigest() == row['sha256']
    assert not (world['new_dir']/'resources/postgres/bin/pg_dump.exe').exists()
    assert program.document(world['new_dir'])['sha256'] == world['previous']['sha256']


def test_a_cross_schema_open_is_impossible_by_construction(tmp_path, monkeypatch):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(
        '{"format": 1, "installation": "%s", "owner": "o", "port": 54329, "phase": "ready"}'
        % INSTALLATION, encoding='utf8')
    world = prepared_phase(root, tmp_path, monkeypatch)
    state, selected, commit = pointer(world['record']['clone']['generation'])
    install_channel(monkeypatch)
    log = []
    wire(monkeypatch, world, log=log,
         allowed={(world['record']['clone']['generation'], 'new')}, profile_tag='new')
    service.rollback_prepare(root, app=world['new_dir'])
    monkeypatch.setattr(service, 'desktop_profile', lambda: Profile('old'))
    monkeypatch.setattr(service, 'executing_app', lambda: world['carrier'])
    monkeypatch.setattr(service.data, 'selection_generation', lambda *a: state['generation'])
    monkeypatch.setattr(service.data, 'selected', selected)
    monkeypatch.setattr(service.data, 'commit_selection', commit)
    with pytest.raises(AssistantError) as error:
        service.rollback(root, installed_app=world['new_dir'])
    assert error.value.code == 'upgrade_database', 'the double refuses the wrong schema'
    assert state['generation'] == world['record']['clone']['generation'], 'the pointer did not move'


def test_an_interrupted_program_write_is_finished_from_the_record(tmp_path, monkeypatch):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(
        '{"format": 1, "installation": "%s", "owner": "o", "port": 54329, "phase": "ready"}'
        % INSTALLATION, encoding='utf8')
    world = prepared_phase(root, tmp_path, monkeypatch)
    state, selected, commit = pointer(world['record']['clone']['generation'])
    install_channel(monkeypatch)
    log = []
    wire(monkeypatch, world, log=log,
         allowed={(world['record']['clone']['generation'], 'new'), (OLD_GENERATION, 'old')},
         profile_tag='new')
    service.rollback_prepare(root, app=world['new_dir'])
    monkeypatch.setattr(service, 'desktop_profile', lambda: Profile('old'))
    monkeypatch.setattr(service, 'executing_app', lambda: world['carrier'])
    monkeypatch.setattr(service.data, 'selection_generation', lambda *a: state['generation'])
    monkeypatch.setattr(service.data, 'selected', selected)
    monkeypatch.setattr(service.data, 'commit_selection', commit)
    real_plan = program.apply_plan

    def half(plan, directory, app):
        first = plan['write'][0]
        real_plan({'write': [first], 'remove': []}, directory, app)
        raise AssistantError('upgrade_program_changed', 'power loss')

    monkeypatch.setattr(service.program, 'apply_plan', half)
    with pytest.raises(AssistantError):
        service.rollback(root, installed_app=world['new_dir'])
    interrupted = journal.load(root)
    assert interrupted['phase'] == 'rolling-back'
    assert interrupted['rollback']['progress']['pointer_switched'] is True
    assert state['generation'] == OLD_GENERATION
    assert (world['new_dir']/'resources/postgres/bin/pg_dump.exe').exists(), \
        'the newer build still has its extra file at this point'
    monkeypatch.setattr(service.program, 'apply_plan', real_plan)
    report = service.rollback(root, installed_app=world['new_dir'])
    assert report['phase'] == 'rolled-back'
    assert report['removed'] == ['resources/postgres/bin/pg_dump.exe']
    assert not (world['new_dir']/'resources/postgres/bin/pg_dump.exe').exists()
    assert program.document(world['new_dir'])['sha256'] == world['previous']['sha256']


def test_the_rollback_refuses_without_the_host_verification_channel(tmp_path, monkeypatch):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(
        '{"format": 1, "installation": "%s", "owner": "o", "port": 54329, "phase": "ready"}'
        % INSTALLATION, encoding='utf8')
    world = prepared_phase(root, tmp_path, monkeypatch)
    log = []
    wire(monkeypatch, world, log=log,
         allowed={(world['record']['clone']['generation'], 'new')}, profile_tag='new')
    with pytest.raises(AssistantError) as error:
        service.rollback_prepare(root, app=world['new_dir'])
    assert error.value.code == 'upgrade_rollback_unavailable'
    assert journal.load(root)['phase'] == 'staged'


def test_the_rollback_refuses_to_run_from_the_directory_it_overwrites(tmp_path, monkeypatch):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(
        '{"format": 1, "installation": "%s", "owner": "o", "port": 54329, "phase": "ready"}'
        % INSTALLATION, encoding='utf8')
    world = prepared_phase(root, tmp_path, monkeypatch)
    install_channel(monkeypatch)
    log = []
    wire(monkeypatch, world, log=log,
         allowed={(world['record']['clone']['generation'], 'new')}, profile_tag='new')
    service.rollback_prepare(root, app=world['new_dir'])
    monkeypatch.setattr(service, 'executing_app', lambda: world['new_dir'])
    with pytest.raises(AssistantError) as error:
        service.rollback(root, installed_app=world['new_dir'])
    assert error.value.code == 'upgrade_executing_program'


def test_the_rollback_refuses_a_program_that_is_not_the_recorded_carrier(tmp_path, monkeypatch):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(
        '{"format": 1, "installation": "%s", "owner": "o", "port": 54329, "phase": "ready"}'
        % INSTALLATION, encoding='utf8')
    world = prepared_phase(root, tmp_path, monkeypatch)
    install_channel(monkeypatch)
    log = []
    wire(monkeypatch, world, log=log,
         allowed={(world['record']['clone']['generation'], 'new')}, profile_tag='new')
    service.rollback_prepare(root, app=world['new_dir'])
    stranger = tmp_path/'stranger'
    stranger.mkdir()
    published(stranger, {'Qiban.exe': b'MZ stranger\n'})
    monkeypatch.setattr(service, 'executing_app', lambda: stranger)
    with pytest.raises(AssistantError) as error:
        service.rollback(root, installed_app=world['new_dir'])
    assert error.value.code == 'upgrade_executing_program'


def test_the_sealed_whitelist_round_trips_and_refuses_a_foreign_key(tmp_path):
    settings = {'desktop-owner': {'installation': INSTALLATION, 'actor': {'platform': 'windows'}},
                'desktop-lock': {'enabled': True, 'revision': 4}}
    sealed = data.seal_settings(settings, b'1'*32, keyref=OLD_GENERATION)
    assert data.unseal_settings(sealed, b'1'*32) == settings
    with pytest.raises(AssistantError) as error:
        data.unseal_settings(sealed, b'2'*32)
    assert error.value.code == 'upgrade_state'
    tampered = {**sealed, 'ciphertext': sealed['ciphertext'][:-8]+'AAAAAAA='}
    with pytest.raises(AssistantError):
        data.unseal_settings(tampered, b'1'*32)


def test_status_is_read_only_even_when_the_record_looks_resumable(tmp_path, monkeypatch):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(
        '{"format": 1, "installation": "%s", "owner": "o", "port": 54329, '
        '"phase": "ready"}' % INSTALLATION, encoding='utf8')
    record = record_at(root, 'staging')
    before = (root/'upgrade'/'transaction.json').read_bytes()
    monkeypatch.setattr(service.data, 'selection_generation',
                        lambda *args: record['plan']['generation'])
    monkeypatch.setattr(service.data, 'selected', lambda *args: {
        'generation': record['plan']['generation'], 'database': record['plan']['database'],
        'store': record['plan']['store'], 'credential_id': record['plan']['credential_id'],
        'manifest_sha256': '5'*64})
    report = service.status(root)
    assert report['phase'] == 'staging', 'the phase is reported as it is'
    assert report['reconcile'] == 'staged', 'the reader is told what a writer would do'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before, 'status wrote the record'


def test_pointer_commit_before_progress_write_can_resume(tmp_path, monkeypatch):
    root = tmp_path / 'Profile'
    (root / 'store/history/portable-preservation').mkdir(parents=True)
    (root / 'installation.json').write_text(
        '{"format":1,"installation":"%s","owner":"o","port":54329,"phase":"ready"}'
        % INSTALLATION, encoding='utf8')
    world = prepared_phase(root, tmp_path, monkeypatch)
    state, selected, commit = pointer(world['record']['clone']['generation'])
    install_channel(monkeypatch)
    log = []
    applied = wire(monkeypatch, world, log=log,
                   allowed={(world['record']['clone']['generation'], 'new'), (OLD_GENERATION, 'old')},
                   profile_tag='new')
    monkeypatch.setattr(service.data, 'selection_generation', lambda *a: state['generation'])
    monkeypatch.setattr(service.data, 'selected', selected)
    monkeypatch.setattr(service.data, 'commit_selection', commit)
    service.rollback_prepare(root, app=world['new_dir'])
    monkeypatch.setattr(service, 'desktop_profile', lambda: Profile('old'))
    monkeypatch.setattr(service, 'executing_app', lambda: world['carrier'])
    advance = journal.advance
    def interrupted(root_value, record, phase, **changes):
        if phase == 'rolling-back' and changes.get('rollback', {}).get('progress', {}).get('pointer_switched'):
            raise OSError('simulated power loss after pointer commit')
        return advance(root_value, record, phase, **changes)
    monkeypatch.setattr(journal, 'advance', interrupted)
    with pytest.raises(OSError):
        service.rollback(root, installed_app=world['new_dir'])
    assert state['generation'] == OLD_GENERATION
    assert journal.load(root)['rollback']['progress']['pointer_switched'] is False
    applications = len(applied)
    monkeypatch.setattr(journal, 'advance', advance)
    report = service.rollback(root, installed_app=world['new_dir'])
    assert report['phase'] == 'rolled-back'
    assert len(applied) == applications
