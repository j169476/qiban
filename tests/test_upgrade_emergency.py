"""The emergency way back: what must be sealed first, and what must never be touched.

The emergency branch exists for one situation - the new version is installed and its own
maintenance backend cannot run, so no fresh logical preservation can be produced - and it is
narrower than a rollback: the faulty installation is sealed offline first, and only then does the
previous program switch back over a persisted plan.

These cases run against real files, the real journal, the real locks and real program trees. Only
three things are replaced, because none of them may run in an offline unit test: the database
session (a strict double that refuses any generation/profile pair it was not built for), the
Windows DPAPI call (a stand-in that records what it was handed and never prints it) and the
"is the cluster stopped" command.
"""
import contextlib
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import upgrade.cleanup as cleanup  # noqa: E402
import upgrade.data as data  # noqa: E402
import upgrade.emergency as emergency  # noqa: E402
import upgrade.journal as journal  # noqa: E402
import upgrade.program as program  # noqa: E402
import upgrade.registry as registry  # noqa: E402
import upgrade.service as service  # noqa: E402
import upgrade.verification as verification  # noqa: E402
from test_upgrade_cleanup_gate import (  # noqa: E402
    INSTALLATION,
    OLD_GENERATION,
    Vault,
    artifact,
    candidate_receipt,
    generation_tree,
    manifest_digest,
    record_at,
    run_clean,
)
from test_upgrade_program import NEW_FILES, OLD_FILES, published  # noqa: E402
from test_upgrade_program_windows_path import (  # noqa: E402
    MAX_PATH,
    at,
    check_watched,
    watch_io,
)

from companion_core.errors import AssistantError  # noqa: E402

TRANSACTION = 'e'*32
LATER_TRANSACTION = 'f'*32
LATER_PROGRAM = 'c'*64
EXTRA = 'resources/postgres/bin/pg_dump.exe'
# The original function object, so the two cases that test it are not affected by the autouse patch.
REAL_STOPPED = emergency._cluster_stopped


# -- doubles ---------------------------------------------------------------


class Profile:
    def __init__(self, tag):
        self.tag = tag


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


class Seal:
    """A stand-in for the per-user DPAPI call: records what it was given, prints nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, raw, binding, decrypt=False):
        self.calls.append({'raw': raw, 'binding': binding, 'decrypt': decrypt})
        return b'DPAPI1:'+hashlib.sha256(raw+binding).digest()


# -- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def local_acl(monkeypatch):
    """The ACL call needs Windows privileges the offline runner does not grant."""
    monkeypatch.setattr(journal, 'protect_directory', lambda path: None)
    monkeypatch.setattr(program, 'protect_directory', lambda path: None)
    monkeypatch.setattr(emergency, 'protect_directory', lambda path: None)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Neither a GUI nor a database server may be reached from a unit test."""
    monkeypatch.setattr(emergency, '_running', lambda: False)
    monkeypatch.setattr(emergency, '_cluster_stopped', lambda root, library: None)


@pytest.fixture(autouse=True)
def channel():
    """The host's Windows verification channel, as a clearly marked test stub."""
    install_channel()
    yield
    verification.clear()


def install_channel():
    # One second in the past: the receipt is fresh, and a coarse system clock cannot make it
    # look like it was taken in the future.
    at = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    verification.install(lambda request: {'session': 'windows-1', 'at': at})


@pytest.fixture
def dpapi(monkeypatch):
    value = Seal()
    monkeypatch.setattr(emergency, 'crypt', value)
    return value


def build_world(tmp_path, *, phase='confirmed'):
    """A real root, a real record in the requested phase, a carrier and the newer build installed."""
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'database').mkdir()
    (root/'database'/'PG_VERSION').write_text('18\n', encoding='utf8')
    (root/'installation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'owner': 'o'*48, 'port': 54329,
         'phase': 'ready'}), encoding='utf8')
    generation_tree(root, OLD_GENERATION)
    old_dir, new_dir, installed = tmp_path/'old', tmp_path/'new', tmp_path/'installed'
    for directory in (old_dir, new_dir, installed):
        directory.mkdir()
    previous = published(old_dir, OLD_FILES)
    target = published(new_dir, NEW_FILES)
    record = journal.create(
        INSTALLATION,
        {'identity_sha256': previous['sha256'],
         'selection': {'generation': OLD_GENERATION,
                       'database': 'qiban_restore_'+OLD_GENERATION,
                       'store': 'generations/'+OLD_GENERATION+'/store',
                       'credential_id': OLD_GENERATION,
                       'manifest_sha256': manifest_digest(root, OLD_GENERATION)},
         'data': {'fingerprint': '1'*64, 'migrations_sha256': '2'*64}},
        {'identity_sha256': target['sha256']},
        {'identity_sha256': previous['sha256'], 'count': previous['count'],
         'bytes': previous['bytes']},
        artifact(root), transaction=TRANSACTION)
    carrier = program.copy_carrier(root, old_dir, previous, installation=INSTALLATION,
                                   transaction=TRANSACTION, roles=['carrier'])
    record = journal.save(root, record)
    plan = data.plan_clone(INSTALLATION, TRANSACTION)
    record = journal.advance(root, record, 'staging', plan=plan)
    record = journal.advance(root, record, 'staged', clone={**plan, 'manifest_sha256': '5'*64})
    if phase == 'confirmed':
        record = journal.advance(root, record, 'confirmed',
                                 confirmed={'identity_sha256': target['sha256'], 'at': 'now',
                                            'data': {'fingerprint': '6'*64,
                                                     'migrations_sha256': '7'*64}})
    for row in target['files']:
        (installed/row['path']).parent.mkdir(parents=True, exist_ok=True)
        (installed/row['path']).write_bytes((new_dir/row['path']).read_bytes())
    (installed/program.IDENTITY).write_bytes((new_dir/program.IDENTITY).read_bytes())
    return {'root': root, 'record': record, 'previous': previous, 'target': target,
            'carrier': carrier, 'installed': installed, 'old_dir': old_dir, 'new_dir': new_dir,
            'clone': plan['generation']}


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def pointer(world):
    """The generation pointer, as a state machine the test can inspect."""
    origin = world['record']['origin']['selection']
    state = {'generation': world['clone']}

    def selected(root_value, installation):
        if state['generation'] == origin['generation']:
            return dict(origin)
        return {'generation': state['generation'],
                'database': 'qiban_restore_'+state['generation'],
                'store': 'generations/'+state['generation']+'/store',
                'credential_id': state['generation'],
                'manifest_sha256': world['record']['clone']['manifest_sha256']}

    def commit(root_value, installation, generation, current):
        assert current['generation'] == state['generation']
        state['generation'] = generation
        return selected(root_value, installation)

    return state, selected, commit


def wire_pointer(monkeypatch, world):
    state, selected, commit = pointer(world)
    monkeypatch.setattr(emergency.data, 'selection_generation', lambda *args: state['generation'])
    monkeypatch.setattr(emergency.data, 'selected', selected)
    monkeypatch.setattr(emergency.data, 'commit_selection', commit)
    return state


def wire_session(monkeypatch, *, allowed, log, holds):
    """One strict store per open, plus recorders for the two recovery rules."""
    monkeypatch.setattr(emergency, 'desktop_profile', lambda: Profile('old'))
    monkeypatch.setattr(emergency, '_hold_session', lambda *args: holds.append('hold'))
    monkeypatch.setattr(emergency, '_retire_tasks', lambda *args: holds.append('retire'))

    @contextlib.contextmanager
    def session(root, app, *, selection=None, profile=None, **kwargs):
        yield (StrictStore(selection['generation'], profile, allowed, log), b'k'*32, {}, {},
               selection)

    monkeypatch.setattr(emergency.data, 'session', session)


def keys(world):
    """One data key per slot this fixture can reach: the installation, the fallback and the clone."""
    return Vault({world['clone']: b'2'*32})


def runner(code):
    """A stand-in for the ``pg_ctl status`` call: its exit code is the whole answer."""
    return SimpleNamespace(run=lambda *args, **kwargs: SimpleNamespace(returncode=code),
                           SubprocessError=RuntimeError, DEVNULL=-3, PIPE=-1, STDOUT=-2)


def sealed(world, vault=None):
    return service.emergency_seal(world['root'], app=world['carrier'],
                                  vault=vault if vault is not None else keys(world))


def switched(world, vault=None):
    return service.emergency_rollback(world['root'], installed_app=world['installed'],
                                      app=world['carrier'],
                                      vault=vault if vault is not None else keys(world))


def archive_of(world):
    return emergency.archive_path(world['root'], world['record']['id'])


# -- one seal, or none -----------------------------------------------------


def test_a_failed_seal_switches_nothing(world, monkeypatch, dpapi):
    """The whole point of sealing first: a copy that did not finish means no switch at all."""
    root = world['root']
    before = (root/'upgrade'/'transaction.json').read_bytes()
    real = emergency._copy

    def failing(source, destination, row):
        if row['path'].startswith('database/'):
            raise AssistantError('upgrade_emergency_copy', 'power loss')
        return real(source, destination, row)

    monkeypatch.setattr(emergency, '_copy', failing)
    with pytest.raises(AssistantError) as error:
        sealed(world)
    assert error.value.code == 'upgrade_emergency_copy'
    assert journal.load(root)['phase'] == 'confirmed', 'the record moved'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before
    assert not (archive_of(world)/emergency.COMPLETE).exists()
    assert not (root/'upgrade'/'emergency'/emergency.PINS).exists()
    assert emergency.pinned(root, INSTALLATION) == (set(), set())
    assert program.document(world['installed'])['sha256'] == world['target']['sha256']
    assert program.document(world['carrier'])['sha256'] == world['previous']['sha256']


def test_a_completed_seal_is_reused_instead_of_being_copied_again(world, monkeypatch, dpapi):
    root = world['root']
    copies = {'n': 0}
    real = emergency._copy

    def counted(source, destination, row):
        copies['n'] += 1
        return real(source, destination, row)

    monkeypatch.setattr(emergency, '_copy', counted)
    first = sealed(world)
    assert first['phase'] == 'emergency' and first['already'] is False
    assert first['material'] == 'fault-material' and first['restorable'] is False
    assert '不是已经可以导入的逻辑保全' in first['note']
    archive = archive_of(world)
    rows = json.loads((archive/emergency.MANIFEST).read_bytes())
    assert copies['n'] == rows['count']-1, 'the sealed credential blob is written, not copied'
    assert {row['path'] for row in rows['files']} >= {
        'database/PG_VERSION', 'installation.json',
        'generations/'+OLD_GENERATION+'/store/history/portable-preservation/pre.ccbackup'}
    for row in rows['files']:
        target = archive.joinpath(*row['path'].split('/'))
        assert target.stat().st_size == row['bytes']
        assert hashlib.sha256(target.read_bytes()).hexdigest() == row['sha256']
    marker = (archive/emergency.COMPLETE).read_bytes()
    pins = json.loads((root/'upgrade'/'emergency'/emergency.PINS).read_bytes())
    assert [entry['transaction'] for entry in pins['archives']] == [TRANSACTION]
    assert pins['archives'][0]['generations'] == [world['clone']]
    assert pins['archives'][0]['programs'] == [world['target']['sha256']]

    copies['n'] = 0
    second = sealed(world)
    assert second['already'] is True and copies['n'] == 0, 'a finished archive is not copied again'
    assert (archive/emergency.COMPLETE).read_bytes() == marker
    assert len(json.loads((root/'upgrade'/'emergency'/emergency.PINS).read_bytes())['archives']) == 1


def test_a_finished_archive_is_reused_when_the_record_write_was_interrupted(world, monkeypatch,
                                                                           dpapi):
    """The marker is the last file of the archive, so a crash before the record write is repairable."""
    root = world['root']
    copies = {'n': 0}
    real = emergency._copy

    def counted(source, destination, row):
        copies['n'] += 1
        return real(source, destination, row)

    advance = journal.advance

    def interrupted(root_value, value, phase, **updates):
        if phase == 'emergency':
            raise OSError('simulated power loss before the record write')
        return advance(root_value, value, phase, **updates)

    monkeypatch.setattr(emergency, '_copy', counted)
    monkeypatch.setattr(journal, 'advance', interrupted)
    with pytest.raises(OSError):
        sealed(world)
    assert journal.load(root)['phase'] == 'confirmed', 'the record was not moved'
    assert (archive_of(world)/emergency.COMPLETE).is_file()
    files = copies['n']

    monkeypatch.setattr(journal, 'advance', advance)
    copies['n'] = 0
    report = sealed(world)
    assert report['phase'] == 'emergency' and report['already'] is False
    assert copies['n'] == 0, 'the finished archive was copied again'
    assert files >= 1
    pins = json.loads((root/'upgrade'/'emergency'/emergency.PINS).read_bytes())
    assert [entry['transaction'] for entry in pins['archives']] == [TRANSACTION]


def test_an_empty_directory_is_archived_so_the_cluster_can_start(world, dpapi):
    """A stopped cluster keeps directories that hold no file; an invented tree cannot start."""
    root = world['root']
    for name in ('database/pg_notify', 'database/pg_replslot', 'database/pg_logical/snapshots'):
        (root/name).mkdir(parents=True, exist_ok=True)
    (root/'logs').mkdir()
    (root/'logs'/'run.log').write_text('transient\n', encoding='utf8')
    sealed(world)
    archive = archive_of(world)
    rows = json.loads((archive/emergency.MANIFEST).read_bytes())
    expected = {'database/pg_notify', 'database/pg_replslot', 'database/pg_logical/snapshots'}
    assert expected <= set(rows['directories'])
    for name in expected:
        folder = archive.joinpath(*name.split('/'))
        assert folder.is_dir() and not list(folder.iterdir()), f'{name} is not an empty directory'
    assert 'logs' not in rows['directories'], 'the exclusion rule applies to directories too'
    assert not any(name.startswith('upgrade') for name in rows['directories'])
    found = sorted(path.relative_to(archive).as_posix()
                   for path in archive.rglob('*') if path.is_dir())
    assert found == rows['directories'], 'the archive holds a directory the manifest omits'


def test_the_switch_refuses_an_archive_without_its_completion_marker(world, dpapi):
    """The way back only exists over a finished seal; the check is one small read."""
    root = world['root']
    sealed(world)
    (archive_of(world)/emergency.COMPLETE).unlink()
    with pytest.raises(AssistantError) as error:
        switched(world)
    assert error.value.code == 'upgrade_emergency_state'
    assert journal.load(root)['phase'] == 'emergency', 'nothing was switched'


def test_the_credential_values_are_sealed_and_never_written_in_clear(world, monkeypatch, dpapi):
    """Sealing is one opaque blob, and the clear text exists only inside the call that made it."""
    root = world['root']
    generation_tree(root, world['clone'], working=False)
    state = wire_pointer(monkeypatch, world)
    report = sealed(world)
    vault = keys(world)
    assert report['slots'] == sorted({INSTALLATION, OLD_GENERATION, world['clone']})
    call = dpapi.calls[0]
    assert call['decrypt'] is False
    assert call['binding'] == emergency.credentials_binding(INSTALLATION, TRANSACTION)
    assert sorted(json.loads(call['raw'])['slots']) == report['slots']
    values = json.loads(call['raw'])['slots']
    for slot in (INSTALLATION, OLD_GENERATION, world['clone']):
        assert values[slot] == vault.get_password(None, slot)
    blob = (archive_of(world)/emergency.CREDENTIALS).read_bytes()
    assert blob == b'DPAPI1:'+hashlib.sha256(call['raw']+call['binding']).digest()
    for path in archive_of(world).rglob('*'):
        if not path.is_file():
            continue
        content = path.read_bytes()
        assert b'data_key' not in content, f'{path.name} holds a credential value'
        assert b'r'*40 not in content and b'p'*40 not in content, f'{path.name} holds a secret'
    assert state['generation'] == world['clone'], 'sealing never moves the pointer'


def test_a_cluster_that_still_answers_is_never_sealed(world, monkeypatch, dpapi):
    """The stopped check is the payload's own ``pg_ctl status`` plus the existing port probe."""
    root = world['root']
    monkeypatch.setattr(emergency.data, '_binaries', lambda app: root)
    monkeypatch.setattr(emergency.data, 'state',
                        lambda value: {'installation': INSTALLATION, 'port': 54329,
                                       'phase': 'ready'})
    monkeypatch.setattr(emergency.data, 'free_port', lambda value: False)
    monkeypatch.setattr(emergency, 'subprocess', runner(3))
    with pytest.raises(AssistantError) as error:
        REAL_STOPPED(root, world['carrier'])
    assert error.value.code == 'upgrade_emergency_running'

    monkeypatch.setattr(emergency.data, 'free_port', lambda value: True)
    monkeypatch.setattr(emergency, 'subprocess', runner(0))
    with pytest.raises(AssistantError) as error:
        REAL_STOPPED(root, world['carrier'])
    assert error.value.code == 'upgrade_emergency_running'

    monkeypatch.setattr(emergency, 'subprocess', runner(3))
    assert REAL_STOPPED(root, world['carrier']) is None

    monkeypatch.setattr(emergency, 'subprocess', runner(4))
    with pytest.raises(AssistantError) as error:
        REAL_STOPPED(root, world['carrier'])
    assert error.value.code == 'upgrade_emergency_stopped'


def test_a_running_application_stops_the_seal(world, monkeypatch, dpapi):
    monkeypatch.setattr(emergency, '_running', lambda: True)
    with pytest.raises(AssistantError) as error:
        sealed(world)
    assert error.value.code == 'upgrade_running'
    assert journal.load(world['root'])['phase'] == 'confirmed'


# -- one switch, from the record -------------------------------------------


def test_the_old_schema_is_the_only_database_that_is_opened(world, monkeypatch, dpapi):
    """Sealing never opens a database; the switch opens the recorded fallback and nothing else."""
    log, holds = [], []
    state = wire_pointer(monkeypatch, world)
    wire_session(monkeypatch, allowed={(OLD_GENERATION, 'old')}, log=log, holds=holds)
    sealed(world)
    assert log == [], 'sealing a faulty installation must not open any database'

    report = switched(world)
    assert log == [(OLD_GENERATION, 'old')], 'the new schema was opened by the old program'
    assert report['phase'] == 'emergency-rolled-back'
    assert state['generation'] == OLD_GENERATION
    assert holds == ['hold', 'retire'], 'platforms are held and unfinished tasks retired'
    assert report['generation'] == OLD_GENERATION
    assert '故障新版最后修改的本机设置没有保留' in ' '.join(report['notes'])


def test_an_interrupted_switch_is_finished_from_the_record(world, monkeypatch, dpapi):
    root = world['root']
    log, holds = [], []
    state = wire_pointer(monkeypatch, world)
    wire_session(monkeypatch, allowed={(OLD_GENERATION, 'old')}, log=log, holds=holds)
    sealed(world)
    real = program.apply_plan

    def half(plan, directory, app):
        real({'write': plan['write'][:1], 'remove': []}, directory, app)
        raise AssistantError('upgrade_program_changed', 'power loss')

    monkeypatch.setattr(emergency.program, 'apply_plan', half)
    with pytest.raises(AssistantError):
        switched(world)
    interrupted = journal.load(root)
    assert interrupted['phase'] == 'emergency-switching'
    assert interrupted['emergency']['progress']['pointer_switched'] is True
    assert state['generation'] == OLD_GENERATION
    assert (world['installed']/EXTRA).exists(), 'the newer build still has its extra file here'

    monkeypatch.setattr(emergency.program, 'apply_plan', real)
    report = switched(world)
    assert report['phase'] == 'emergency-rolled-back'
    assert report['removed'] == [EXTRA]
    assert not (world['installed']/EXTRA).exists()
    assert program.document(world['installed'])['sha256'] == world['previous']['sha256']
    assert log == [(OLD_GENERATION, 'old')]


def test_the_pointer_commit_before_the_progress_write_is_not_applied_twice(world, monkeypatch,
                                                                          dpapi):
    root = world['root']
    log, holds = [], []
    state = wire_pointer(monkeypatch, world)
    wire_session(monkeypatch, allowed={(OLD_GENERATION, 'old')}, log=log, holds=holds)
    sealed(world)
    advance = journal.advance

    def interrupted(root_value, value, phase, **updates):
        progress = updates.get('emergency', {}).get('progress') or {}
        if phase == 'emergency-switching' and progress.get('pointer_switched'):
            raise OSError('simulated power loss after the pointer commit')
        return advance(root_value, value, phase, **updates)

    monkeypatch.setattr(journal, 'advance', interrupted)
    with pytest.raises(OSError):
        switched(world)
    assert state['generation'] == OLD_GENERATION
    assert journal.load(root)['emergency']['progress']['pointer_switched'] is False

    monkeypatch.setattr(journal, 'advance', advance)
    report = switched(world)
    assert report['phase'] == 'emergency-rolled-back'
    assert holds == ['hold', 'retire'], 'the recovery rules are not applied a second time'


def test_the_target_manifest_plans_the_removal_and_its_bytes_are_not_checked(world, monkeypatch,
                                                                            dpapi):
    """A damaged main program is exactly why this path exists: only its manifest is read."""
    log, holds = [], []
    wire_pointer(monkeypatch, world)
    wire_session(monkeypatch, allowed={(OLD_GENERATION, 'old')}, log=log, holds=holds)
    (world['installed']/'Qiban.exe').write_bytes(b'MZ the newer build, now damaged\n')
    sealed(world)
    report = switched(world)
    assert report['removed'] == [EXTRA]
    assert report['program_files'] == 'complete'
    assert report['retained']['count'] == 0
    assert not (world['installed']/EXTRA).exists()
    assert program.document(world['installed'])['sha256'] == world['previous']['sha256']
    for row in world['previous']['files']:
        assert hashlib.sha256((world['installed']/row['path']).read_bytes()).hexdigest() == \
            row['sha256']


def test_a_missing_target_manifest_keeps_unknown_files_and_says_so(world, monkeypatch, dpapi):
    """Without that document no directory is guessed at: only the old published set is written."""
    root = world['root']
    log, holds = [], []
    wire_pointer(monkeypatch, world)
    wire_session(monkeypatch, allowed={(OLD_GENERATION, 'old')}, log=log, holds=holds)
    (world['installed']/program.IDENTITY).unlink()
    (world['installed']/'stale.dll').write_bytes(b'left behind by the newer build\n')
    sealed(world)
    report = switched(world)
    assert report['removed'] == [], 'nothing is deleted when the newer manifest is unknown'
    assert report['program_files'] == 'partial'
    assert report['retained']['count'] == 2
    assert sorted(report['retained']['paths']) == sorted([EXTRA, 'stale.dll'])
    assert '旧版本可能因此无法正常运行' in ' '.join(report['notes'])
    assert (world['installed']/EXTRA).exists() and (world['installed']/'stale.dll').exists()
    target = journal.load(root)['emergency']['target']
    assert target['identity_sha256'] is None and target['files'] == 0
    assert target['retained']['paths'] == sorted([EXTRA, 'stale.dll'])
    for row in world['previous']['files']:
        assert hashlib.sha256((world['installed']/row['path']).read_bytes()).hexdigest() == \
            row['sha256']


def test_the_switch_refuses_before_a_completed_seal(world, monkeypatch, dpapi):
    with pytest.raises(AssistantError) as error:
        switched(world)
    assert error.value.code == 'upgrade_state'
    assert journal.load(world['root'])['phase'] == 'confirmed'


def test_both_emergency_verbs_require_the_host_verification_channel(world, dpapi):
    root = world['root']
    before = (root/'upgrade'/'transaction.json').read_bytes()
    verification.clear()
    with pytest.raises(AssistantError) as error:
        sealed(world)
    assert error.value.code == 'upgrade_rollback_unavailable'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before
    install_channel()
    sealed(world)
    verification.clear()
    with pytest.raises(AssistantError) as error:
        switched(world)
    assert error.value.code == 'upgrade_rollback_unavailable'
    assert journal.load(root)['phase'] == 'emergency', 'nothing was switched without a receipt'


def test_the_verbs_run_only_from_the_recorded_carrier(world, dpapi):
    stranger = world['installed']
    with pytest.raises(AssistantError) as error:
        service.emergency_seal(world['root'], app=stranger, vault=Vault({}))
    assert error.value.code == 'upgrade_executing_program'
    with pytest.raises(AssistantError) as error:
        service.emergency_rollback(world['root'], installed_app=stranger, app=stranger,
                                   vault=Vault({}))
    assert error.value.code == 'upgrade_executing_program'
    assert journal.load(world['root'])['phase'] == 'confirmed'


# -- what a later cleanup must still keep ----------------------------------


def test_a_later_cleanup_still_protects_the_sealed_generation_and_build(world, monkeypatch, dpapi):
    """Cleanup of a *finished, later* upgrade would otherwise drop both; the pin keeps them."""
    root = world['root']
    sealed(world)
    log, holds = [], []
    wire_pointer(monkeypatch, world)
    wire_session(monkeypatch, allowed={(OLD_GENERATION, 'old')}, log=log, holds=holds)
    switched(world)
    assert journal.load(root)['phase'] == 'emergency-rolled-back'

    # A newer upgrade finished afterwards; its own record knows nothing about the sealed ones.
    origin = {'identity_sha256': 'b'*64,
              'selection': {'generation': OLD_GENERATION,
                            'database': 'qiban_restore_'+OLD_GENERATION,
                            'store': 'generations/'+OLD_GENERATION+'/store',
                            'credential_id': OLD_GENERATION,
                            'manifest_sha256': manifest_digest(root, OLD_GENERATION)},
              'data': {'fingerprint': '1'*64, 'migrations_sha256': '2'*64}}
    record = journal.create(INSTALLATION, origin, {'identity_sha256': LATER_PROGRAM},
                            {'identity_sha256': 'b'*64, 'count': 3, 'bytes': 30},
                            artifact(root), transaction=LATER_TRANSACTION)
    record = journal.save(root, record)
    plan = data.plan_clone(INSTALLATION, LATER_TRANSACTION)
    record = journal.advance(root, record, 'staging', plan=plan)
    record = journal.advance(root, record, 'staged', clone={**plan, 'manifest_sha256': '5'*64})
    journal.advance(root, record, 'confirmed',
                    confirmed={'identity_sha256': LATER_PROGRAM, 'at': 'now',
                               'data': {'fingerprint': '6'*64, 'migrations_sha256': '7'*64}})
    generation_tree(root, world['clone'], working=True)
    registry.register_generation(
        root, INSTALLATION,
        {'generation': world['clone'], 'database': 'qiban_restore_'+world['clone'],
         'store': 'generations/'+world['clone']+'/store', 'credential_id': world['clone']},
        LATER_TRANSACTION, manifest_sha256=manifest_digest(root, world['clone']),
        preservation=candidate_receipt(root, world['clone']), source_fingerprint='9'*64)
    program.copy_carrier(root, world['new_dir'], world['target'], installation=INSTALLATION,
                         transaction=TRANSACTION, roles=['target'])
    registry.register_program(root, INSTALLATION, world['target'], TRANSACTION, 'target')
    monkeypatch.setattr(cleanup.data, 'verify_artifact', lambda *args, **kwargs: None)
    monkeypatch.setattr(cleanup.data, 'selection_generation', lambda *args: plan['generation'])
    report, dropped = run_clean(root, monkeypatch,
                                vault=Vault({world['clone']: b'2'*32}))

    assert dropped == [], 'the sealed generation was dropped'
    assert report['complete'] is True, report['cleanup']['failed']
    reasons = [entry['reason'] for entry in report['cleanup']['skipped']
               if entry.get('generation') == world['clone']]
    assert reasons == ['本机紧急封存仍需要这个代次，不能清理。']
    assert world['clone'] in {entry['generation']
                              for entry in registry.load(root, INSTALLATION)['generations']}
    store = root/'generations'/world['clone']/'store'
    assert (store/'assets'/'a.png').is_file()
    kept = [entry for entry in report['cleanup']['skipped']
            if entry.get('program') == world['target']['sha256']]
    assert kept and kept[0]['reason'] == '本机紧急封存仍需要这个程序副本，不能清理。'
    assert program.tree(root, world['target']['sha256']).is_dir()
    assert archive_of(world).is_dir(), 'the archive has no automatic deletion path'
    assert emergency.pinned(root, INSTALLATION)[0] == {world['clone']}


# -- the record itself -----------------------------------------------------


def test_the_emergency_branch_cannot_be_attached_to_a_normal_phase(tmp_path, monkeypatch):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'owner': 'o'*48, 'port': 54329,
         'phase': 'ready'}), encoding='utf8')
    generation_tree(root, OLD_GENERATION)
    record = record_at(root, 'staged')
    block = {'at': 'now', 'receipt': {'session': 's', 'at': 'now'},
             'archive': {'transaction': record['id'], 'manifest_sha256': 'a'*64,
                         'count': 1, 'bytes': 2},
             'target': None, 'progress': None}
    with pytest.raises(AssistantError) as error:
        journal.save(root, {**record, 'emergency': block})
    assert error.value.code == 'upgrade_journal_invalid'


def test_the_emergency_branch_has_its_own_legal_successors_and_parts(tmp_path):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'owner': 'o'*48, 'port': 54329,
         'phase': 'ready'}), encoding='utf8')
    generation_tree(root, OLD_GENERATION)
    record = record_at(root, 'staged')
    block = {'at': 'now', 'receipt': {'session': 's', 'at': 'now'},
             'archive': {'transaction': record['id'], 'manifest_sha256': 'a'*64,
                         'count': 1, 'bytes': 2},
             'target': None, 'progress': None}
    moved = journal.advance(root, record, 'emergency', emergency=block)
    assert moved['phase'] == 'emergency'
    with pytest.raises(AssistantError) as error:
        journal.advance(root, moved, 'emergency-rolled-back')
    assert error.value.code == 'upgrade_state', 'the switch cannot be skipped'
    progress = {'at': 'now', 'pointer_before': record['clone']['generation'],
                'pointer_switched': False,
                'previous_files': [{'path': 'Qiban.exe', 'bytes': 1, 'sha256': 'a'*64}],
                'target_files': [{'path': 'Qiban.exe', 'bytes': 1, 'sha256': 'a'*64}],
                'previous_identity': record['carrier']['identity_sha256'],
                'target_identity': record['target']['identity_sha256']}
    switching = journal.advance(root, moved, 'emergency-switching',
                                emergency={**block, 'progress': progress,
                                           'target': {'identity_sha256': 'b'*64, 'files': 1,
                                                      'retained': {'count': None, 'paths': [],
                                                                   'truncated': False}}})
    assert switching['emergency']['progress']['pointer_switched'] is False
    with pytest.raises(AssistantError):
        journal.advance(root, switching, 'emergency-rolled-back')
    done = journal.advance(root, switching, 'emergency-switching',
                           emergency={**switching['emergency'],
                                      'progress': {**progress, 'pointer_switched': True}})
    finished = journal.advance(root, done, 'emergency-rolled-back')
    assert finished['phase'] == 'emergency-rolled-back'
    assert finished['rollback'] is None and finished['emergency']['target'] is not None


def test_a_record_from_before_the_emergency_branch_still_loads(tmp_path):
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'owner': 'o'*48, 'port': 54329,
         'phase': 'ready'}), encoding='utf8')
    generation_tree(root, OLD_GENERATION)
    record = record_at(root, 'confirmed')
    legacy = {key: value for key, value in record.items() if key != 'emergency'}
    (root/'upgrade'/'transaction.json').write_text(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True), encoding='utf8')
    loaded = journal.load(root, required=True)
    assert loaded['emergency'] is None and loaded['phase'] == 'confirmed'


@pytest.mark.skipif(os.name != 'nt', reason='the extended path form is a Windows rule')
def test_the_archive_is_written_and_cleared_past_max_path(tmp_path, monkeypatch, dpapi):
    """An archive carries files deeper than MAX_PATH: sealing, proving and re-sealing must work.

    The profile itself stays legal - the customer machine's own profile is longer than a test's, so
    its archive passes MAX_PATH while the profile does not - and only the archive's contents are
    deep. That is the shape the emergency seal meets: ``upgrade/emergency/<transaction>`` plus a
    generation's ``store/history/portable-preservation/<name>.ccbackup``.
    """
    holder = at(tmp_path, 'deep', 152)              # `<holder>/Profile` is 160 characters
    world = build_world(holder)
    root, archive = world['root'], archive_of(world)
    source = root/'generations'/OLD_GENERATION/'store'/'history'/'portable-preservation'/'pre.ccbackup'
    relative = 'generations/'+OLD_GENERATION+'/store/history/portable-preservation/pre.ccbackup'
    empty = root/'generations'/OLD_GENERATION/'store'/'history'/'empty-one'/'empty-two'
    empty.mkdir(parents=True)
    assert len(str(source)) <= MAX_PATH < len(str(archive.joinpath(*relative.split('/')))), \
        'the source has to stay legal while its archived copy is past MAX_PATH'

    # An interrupted seal of this same transaction left an unfinished tree behind. Clearing it is
    # the only thing this call may remove, and the tree is deeper than MAX_PATH as well.
    stray = archive/'generations'/OLD_GENERATION/'store'/'stray-deeper'/'stale.txt'
    os.makedirs(program.native_io_path(stray.parent))
    with open(program.native_io_path(stray), 'wb') as stream:
        stream.write(b'stale')
    assert len(str(stray)) > MAX_PATH

    state = watch_io(monkeypatch)
    sealed(world)
    check_watched(state)
    assert not os.path.exists(program.native_io_path(stray)), 'the unfinished tree was not cleared'
    assert (archive/emergency.COMPLETE).is_file()
    rows = json.loads((archive/emergency.MANIFEST).read_bytes())
    assert relative in {row['path'] for row in rows['files']}
    copy = archive.joinpath(*relative.split('/'))
    with open(program.native_io_path(copy), 'rb') as stream:
        archived = stream.read()
    with open(program.native_io_path(source), 'rb') as stream:
        original = stream.read()
    assert archived == original
    assert hashlib.sha256(archived).hexdigest() == next(
        row['sha256'] for row in rows['files'] if row['path'] == relative)
    empty_row = 'generations/'+OLD_GENERATION+'/store/history/empty-one/empty-two'
    assert empty_row in rows['directories']
    assert not list(os.scandir(program.native_io_path(archive.joinpath(*empty_row.split('/')))))
