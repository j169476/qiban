"""A retry of an open upgrade transaction belongs to the build that transaction was prepared for.

`prepare` hands an unfinished transaction to whoever calls it next, which is what makes a retry
after a crash work - and it is also the one place where a *different* package could otherwise
inherit somebody else's clone and progress. Both halves are pinned here on a real root, a real
record and a real published program directory: the same target resumes without cloning or writing
anything, a different target is refused with `upgrade_target_mismatch` before any state advances,
including a `staging` record the resume would otherwise have pushed to `staged`.

The same target has three shapes, and the second half of this file pins all of them: a `staged`
transaction (or one whose pointer already moved) is continued; a half-finished `prepared`/`staging`
attempt - which nobody could finish and no user-facing abort reaches - is dropped through the steps
`abort` uses and replaced; a transaction that is already going back is refused, never dropped. A
pointer that names some other generation stops the cleanup instead of authorizing it, and a cleanup
that fails leaves the record exactly as it was, so the retry can still work. The frozen maintenance
entry is pinned too: it may only act on the program whose code it is running.
"""
import contextlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import upgrade.journal as journal  # noqa: E402
import upgrade.program as program  # noqa: E402
import upgrade.registry as registry  # noqa: E402
import upgrade.service as service  # noqa: E402
from test_upgrade_cleanup_gate import (  # noqa: E402
    INSTALLATION,
    OLD_GENERATION,
    generation_tree,
    record_at,
)
from test_upgrade_program import OLD_FILES, published  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402

# `record_at` records this as the target program of the transaction it builds.
TARGET = 'c'*64
OTHER = 'd'*64
FOREIGN = 'f'*64
NEW_TRANSACTION = '9'*32
NEW_GENERATION = '8'*32


@pytest.fixture(autouse=True)
def local_acl(monkeypatch):
    """The ACL call needs Windows privileges the offline runner does not grant."""
    monkeypatch.setattr(journal, 'protect_directory', lambda path: None)
    monkeypatch.setattr(program, 'protect_directory', lambda path: None)
    # These tests exercise isolated transaction records, independently of a desktop
    # instance running elsewhere in the same Windows session.
    monkeypatch.setattr(service, 'running', lambda: False)


def make_root(tmp_path):
    """A ready root with one generation, plus an installed program directory of its own."""
    root = tmp_path/'Profile'
    (root/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (root/'installation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'owner': 'o'*48, 'port': 54329,
         'phase': 'ready'}), encoding='utf8')
    generation_tree(root, OLD_GENERATION)
    app = tmp_path/'installed'
    app.mkdir()
    assert published(app, OLD_FILES)['sha256'] != TARGET
    return root, app


def forbid_execution(monkeypatch):
    """Resuming must not clone, open a database or touch the pointer."""

    def explode(name):
        def raised(*_args, **_kwargs):
            raise AssertionError(f'prepare executed {name} while resuming')
        return raised

    for name in ('session', 'clone', 'commit_selection'):
        monkeypatch.setattr(service.data, name, explode('data.'+name))
    monkeypatch.setattr(service.program, 'copy_carrier', explode('program.copy_carrier'))


def test_a_retry_with_the_same_target_resumes_without_cloning(tmp_path, monkeypatch):
    root, app = make_root(tmp_path)
    record = record_at(root, 'staged')
    before = (root/'upgrade'/'transaction.json').read_bytes()
    forbid_execution(monkeypatch)
    report = service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert report['resumed'] is True
    assert report['phase'] == 'staged'
    assert report['transaction'] == record['id']
    assert report['generation'] == record['clone']['generation']
    assert (root/'upgrade'/'transaction.json').read_bytes() == before, 'the retry wrote the record'
    assert journal.load(root)['id'] == record['id']


def test_a_different_target_is_refused_before_any_state_advances(tmp_path, monkeypatch):
    root, app = make_root(tmp_path)
    record = record_at(root, 'staging')
    before = (root/'upgrade'/'transaction.json').read_bytes()
    # This pointer already names the clone, so continuing the record would have advanced it.
    selected = {'generation': record['plan']['generation'], 'database': record['plan']['database'],
                'store': record['plan']['store'], 'credential_id': record['plan']['credential_id'],
                'manifest_sha256': '5'*64}
    monkeypatch.setattr(service.data, 'selection_generation',
                        lambda *args: record['plan']['generation'])
    monkeypatch.setattr(service.data, 'selected', lambda *args: selected)
    forbid_execution(monkeypatch)
    with pytest.raises(AssistantError) as error:
        service.prepare(root, app, target_identity=OTHER)
    assert error.value.code == 'upgrade_target_mismatch'
    assert journal.load(root)['phase'] == 'staging', 'the record was advanced'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before
    # The fixture really is resumable: the same target advances the very same record.
    resumed = service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert resumed['resumed'] is True and resumed['phase'] == 'staged'
    assert journal.load(root)['phase'] == 'staged'


# -- the same target, with nobody to finish the half-finished attempt ------


@contextlib.contextmanager
def fake_admin(root, *, app=None):
    """The maintenance connection `_drop_incomplete` opens; no database is contacted here."""
    yield {'root': root, 'app': app}


def drop_recorder(monkeypatch):
    dropped = []
    monkeypatch.setattr(service.data, 'admin', fake_admin)
    monkeypatch.setattr(service.data, 'drop_database',
                        lambda session, database: dropped.append(database) or
                        {'database': database, 'dropped': True})
    return dropped


def start_recorder(monkeypatch):
    """The rebuild is `_start`'s own job; this pins only that the caller reaches it."""
    calls = []

    def fake(root, app, target, **kwargs):
        calls.append({'app': app, 'target': target, 'kwargs': sorted(kwargs)})
        return {'ok': True, 'phase': 'staged', 'transaction': NEW_TRANSACTION,
                'generation': NEW_GENERATION, 'carrier': TARGET, 'preservation': 'p.ccbackup'}

    monkeypatch.setattr(service, '_start', fake)
    return calls


def test_a_half_finished_staging_attempt_is_dropped_and_rebuilt(tmp_path, monkeypatch):
    """No user-facing abort reaches this state: preparing again is the exit, and it is the same one."""
    root, app = make_root(tmp_path)
    record = record_at(root, 'staging')
    plan = record['plan']
    generation_tree(root, plan['generation'])
    registry.register_generation(root, INSTALLATION, plan, record['id'])
    dropped = drop_recorder(monkeypatch)
    monkeypatch.setattr(service.data, 'selection_generation', lambda *args: OLD_GENERATION)
    calls = start_recorder(monkeypatch)
    report = service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert report['transaction'] == NEW_TRANSACTION, 'the old attempt was not replaced'
    assert calls and calls[0]['target'] == record['target']['identity_sha256']
    assert dropped == [plan['database']], 'the half-finished clone was not dropped'
    assert not (root/'generations'/plan['generation']).exists()
    assert registry.load(root, INSTALLATION)['generations'] == []
    assert report['abandoned'] == {'generation': plan['generation'], 'database': plan['database'],
                                   'dropped': {'database': plan['database'], 'dropped': True},
                                   'directory_removed': True}
    assert journal.load(root)['phase'] == 'aborted', 'the old attempt was not terminated'


def test_a_half_finished_prepared_attempt_is_dropped_without_touching_generations(tmp_path,
                                                                                 monkeypatch):
    root, app = make_root(tmp_path)
    record = record_at(root, 'prepared')
    monkeypatch.setattr(service, '_drop_incomplete',
                        lambda *args, **kwargs: pytest.fail('nothing was staged, nothing to drop'))
    monkeypatch.setattr(service.data, 'selection_generation', lambda *args: OLD_GENERATION)
    calls = start_recorder(monkeypatch)
    report = service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert 'abandoned' not in report
    assert report['transaction'] == NEW_TRANSACTION
    assert journal.load(root)['phase'] == 'aborted'
    assert calls


def test_a_prepared_attempt_with_a_foreign_pointer_is_not_abandoned(tmp_path, monkeypatch):
    """Abandoning is a state change too, so the origin check guards it exactly as it guards a drop."""
    root, app = make_root(tmp_path)
    record = record_at(root, 'prepared')
    before = (root/'upgrade'/'transaction.json').read_bytes()
    monkeypatch.setattr(service.data, 'selection_generation', lambda *args: FOREIGN)
    calls = start_recorder(monkeypatch)
    with pytest.raises(AssistantError) as error:
        service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert error.value.code == 'upgrade_state'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before
    assert journal.load(root)['phase'] == 'prepared', 'the attempt was abandoned anyway'
    assert calls == []


def test_a_pointer_that_names_another_generation_stops_the_cleanup(tmp_path, monkeypatch):
    root, app = make_root(tmp_path)
    record = record_at(root, 'staging')
    plan = record['plan']
    directory = generation_tree(root, plan['generation'])
    before = (root/'upgrade'/'transaction.json').read_bytes()
    drop_recorder(monkeypatch)
    monkeypatch.setattr(service.data, 'drop_database',
                        lambda *args: pytest.fail('a foreign pointer must not authorize a drop'))
    monkeypatch.setattr(service.data, 'selection_generation', lambda *args: FOREIGN)
    calls = start_recorder(monkeypatch)
    with pytest.raises(AssistantError) as error:
        service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert error.value.code == 'upgrade_state'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before
    assert directory.is_dir()
    assert calls == []


def test_a_transaction_going_back_is_refused_instead_of_dropped(tmp_path, monkeypatch):
    root, app = make_root(tmp_path)
    record = record_at(root, 'rollback-ready')
    before = (root/'upgrade'/'transaction.json').read_bytes()
    monkeypatch.setattr(service, '_drop_incomplete',
                        lambda *args, **kwargs: pytest.fail('a transaction going back is not dropped'))
    calls = start_recorder(monkeypatch)
    with pytest.raises(AssistantError) as error:
        service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert error.value.code == 'upgrade_pending'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before
    assert calls == []


def test_a_failed_cleanup_keeps_the_record_retryable(tmp_path, monkeypatch):
    root, app = make_root(tmp_path)
    record = record_at(root, 'staging')
    plan = record['plan']
    generation_tree(root, plan['generation'])
    before = (root/'upgrade'/'transaction.json').read_bytes()
    drop_recorder(monkeypatch)
    monkeypatch.setattr(service.data, 'selection_generation', lambda *args: OLD_GENERATION)
    calls = start_recorder(monkeypatch)

    def offline(*_args):
        raise AssistantError('upgrade_database', '本机数据库不可用')

    monkeypatch.setattr(service.data, 'drop_database', offline)
    with pytest.raises(AssistantError) as error:
        service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert error.value.code == 'upgrade_database'
    assert journal.load(root)['phase'] == 'staging', 'a failed cleanup advanced the record'
    assert (root/'upgrade'/'transaction.json').read_bytes() == before
    assert calls == []
    monkeypatch.setattr(service.data, 'drop_database',
                        lambda session, database: {'database': database, 'dropped': True})
    report = service.prepare(root, app, target_identity=record['target']['identity_sha256'])
    assert report['transaction'] == NEW_TRANSACTION
    assert journal.load(root)['phase'] == 'aborted'


def test_the_frozen_maintenance_entry_only_acts_on_its_own_program(tmp_path, monkeypatch):
    import argparse

    import storage_worker

    own, other = tmp_path/'Own', tmp_path/'Other'
    own.mkdir()
    other.mkdir()
    called = []
    monkeypatch.setattr(service, 'prepare',
                        lambda *args, **kwargs: called.append('prepare') or {'ok': True})
    monkeypatch.setattr(service, 'confirm',
                        lambda *args, **kwargs: called.append('confirm') or {'ok': True})
    monkeypatch.setattr(service, 'executing_app', lambda: own)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    for verb in ('prepare', 'confirm'):
        with pytest.raises(AssistantError) as error:
            storage_worker.maintenance(service, argparse.Namespace(
                data_dir=str(tmp_path), app_dir=str(other), upgrade_maintenance=verb,
                target_identity=TARGET))
        assert error.value.code == 'upgrade_executing_program'
    assert called == [], 'a frozen worker acted on another program directory'
    assert storage_worker.maintenance(service, argparse.Namespace(
        data_dir=str(tmp_path), app_dir=str(own), upgrade_maintenance='prepare',
        target_identity=TARGET)) == {'ok': True}
    assert called == ['prepare']
    # A source-mode run has no program directory of its own to derive; the path is the caller's.
    monkeypatch.delattr(sys, 'frozen', raising=False)
    assert storage_worker.maintenance(service, argparse.Namespace(
        data_dir=str(tmp_path), app_dir=str(other), upgrade_maintenance='confirm')) == {'ok': True}
    assert called == ['prepare', 'confirm']
