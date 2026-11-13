"""The startup decision on an unfinished upgrade: confirm it, or refuse to open.

Every case here is a real installation root and a real transaction record advanced through the
real journal API. The confirm verb itself is replaced by a recorder in the cases about the
decision (which phase may be confirmed, which program directory is used, what a leftover cleanup
becomes), so the assertions are about the decision, and about the confirmation never being called
where business must not start.
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))

import storage_worker  # noqa: E402
import test_upgrade_cleanup_gate as fixtures  # noqa: E402
import upgrade.journal as journal  # noqa: E402
import upgrade.service as service  # noqa: E402
import upgrade.startup as startup  # noqa: E402
from test_upgrade_cleanup_gate import (  # noqa: E402
    CLONE_TRANSACTION,
    NEW_PROGRAM,
    OLD_GENERATION,
    OLD_PROGRAM,
    artifact,
    record_at,
)

from companion_core.errors import AssistantError  # noqa: E402

root = fixtures.root
local_acl = fixtures.local_acl


def forbidden(*args, **kwargs):
    raise AssertionError('a start with nothing to confirm must not hash the program')


def progress(generation):
    """One written-back manifest pair, as an interrupted rollback leaves it in the record."""
    rows = [{'path': 'Qiban.exe', 'bytes': 1, 'sha256': 'a'*64}]
    return {'at': 'now', 'pointer_before': generation, 'pointer_switched': True,
            'previous_files': rows, 'target_files': rows, 'previous_identity': OLD_PROGRAM,
            'target_identity': NEW_PROGRAM}


def rolling_back(root):
    ready = record_at(root, 'rollback-ready')
    return journal.advance(root, ready, 'rolling-back',
                           rollback={**ready['rollback'],
                                     'progress': progress(ready['clone']['generation'])})


def rolled_back(root):
    return journal.advance(root, rolling_back(root), 'rolled-back')


def test_a_root_without_a_record_starts_without_hashing_the_program(root, monkeypatch):
    monkeypatch.setattr(service, 'confirm', forbidden)
    monkeypatch.setattr(service.program, 'verify', forbidden)
    monkeypatch.setattr(service.program, 'document', forbidden)
    assert startup.confirm(root) == {'ready': True, 'action': 'none', 'phase': None,
                                     'warning': None}


@pytest.mark.parametrize('phase', ['confirmed', 'rolled-back', 'aborted'])
def test_a_finished_upgrade_starts_without_hashing_the_program(root, local_acl, monkeypatch, phase):
    record = rolled_back(root) if phase == 'rolled-back' else (
        journal.advance(root, record_at(root, 'prepared'), 'aborted') if phase == 'aborted'
        else record_at(root, 'confirmed'))
    assert record['phase'] == phase
    monkeypatch.setattr(service, 'confirm', forbidden)
    monkeypatch.setattr(service.program, 'verify', forbidden)
    assert startup.confirm(root) == {'ready': True, 'action': 'none', 'phase': phase,
                                     'warning': None}


@pytest.mark.parametrize('phase,code', [
    ('prepared', 'upgrade_maintenance_in_progress'),
    ('staging', 'upgrade_maintenance_in_progress'),
    ('rollback-ready', 'upgrade_rollback_pending'),
    ('rolling-back', 'upgrade_rollback_pending'),
])
def test_an_unfinished_upgrade_refuses_to_open_business(root, local_acl, monkeypatch, phase, code):
    """Only `staged` may be confirmed: the other unfinished phases stop the start, unchanged."""
    monkeypatch.setattr(service, 'confirm', forbidden)
    if phase == 'rolling-back':
        rolling_back(root)
    else:
        record_at(root, phase)
    with pytest.raises(AssistantError) as error:
        startup.confirm(root)
    assert error.value.code == code
    assert journal.load(root)['phase'] == phase, 'a refusal never moves the record'


def test_a_staged_upgrade_is_confirmed_by_the_program_that_is_running(root, local_acl,
                                                                     monkeypatch, tmp_path):
    record_at(root, 'staged')
    running = tmp_path/'installed'
    running.mkdir()
    seen = {}

    def confirm(root_value, app, *, vault=None):
        seen.update(root=root_value, app=app)
        return {'ok': True, 'phase': 'confirmed', 'transaction': CLONE_TRANSACTION,
                'identity_sha256': NEW_PROGRAM,
                'cleanup': {'ok': True, 'complete': True, 'cleanup': {'failed': []}}}

    monkeypatch.setattr(service, 'executing_app', lambda: running)
    monkeypatch.setattr(service, 'confirm', confirm)
    assert startup.confirm(root) == {'ready': True, 'action': 'confirm', 'phase': 'confirmed',
                                     'transaction': CLONE_TRANSACTION, 'warning': None}
    assert seen['root'] == root
    assert seen['app'] == running, 'the program directory is the running one, never an argument'


@pytest.mark.parametrize('cleanup,warning_code', [
    ({'ok': False, 'phase': 'confirmed', 'complete': False,
      'cleanup': {'generations': [], 'programs': [],
                  'skipped': [{'generation': None, 'code': 'upgrade_state',
                               'reason': '最近一次回退目标不能清理。'}],
                  'failed': [{'generation': OLD_GENERATION, 'code': 'upgrade_preserve',
                              'reason': '保全备份校验失败'}]}},
     'upgrade_cleanup_incomplete'),
    ({'ok': False, 'error': 'upgrade_busy', 'reason': '清理期间无法取得维护锁'},
     'upgrade_cleanup_failed'),
])
def test_a_confirmed_upgrade_keeps_an_unfinished_cleanup_as_a_warning(root, local_acl, monkeypatch,
                                                                     cleanup, warning_code):
    """Cleanup left work behind: the upgrade is confirmed, and its reasons are kept, not dropped.

    The leftover shape is the one ``cleanup.clean`` really returns - ``complete: false`` *and*
    ``ok: false`` with the report under ``cleanup`` - so reading the wrapper first would lose the
    per-artifact reasons. The other shape is a cleanup that could not run at all.
    """
    record_at(root, 'staged')
    monkeypatch.setattr(service, 'executing_app', lambda: Path('C:/installed'))
    monkeypatch.setattr(service, 'confirm', lambda *args, **kwargs: {
        'ok': True, 'phase': 'confirmed', 'transaction': CLONE_TRANSACTION,
        'identity_sha256': NEW_PROGRAM, 'cleanup': cleanup})
    result = startup.confirm(root)
    assert result['ready'] is True, 'a finished upgrade is not a failed start'
    assert result['action'] == 'confirm' and result['phase'] == 'confirmed'
    warning = result['warning']
    assert warning['code'] == warning_code
    if 'cleanup' in cleanup:
        assert warning['failed'] == cleanup['cleanup']['failed'], 'the reasons must survive'
        assert warning['skipped'] == cleanup['cleanup']['skipped'], 'a kept generation is not a failure'
    else:
        assert (warning['error'], warning['reason']) == ('upgrade_busy', '清理期间无法取得维护锁')
    assert '可以再次清理' in warning['message']
    assert '自动重试' not in warning['message'], 'no retry may be promised that does not exist'


def recorded_cleanup(root, *, complete, failed):
    """The cleanup result a confirmed record really carries after a run that left work behind."""
    return journal.advance(root, record_at(root, 'confirmed'), 'confirmed',
                           cleanup={'at': 'now', 'generations': [], 'programs': [], 'skipped': [],
                                    'failed': failed, 'complete': complete})


def test_a_second_start_maps_the_recorded_cleanup_warning_without_redoing_it(root, local_acl,
                                                                            monkeypatch):
    """Confirmed and unfinished: ready at once, the recorded reasons shown, nothing hashed or rerun."""
    failed = [{'generation': None, 'code': 'upgrade_preserve', 'reason': '保全备份校验失败'}]
    record = recorded_cleanup(root, complete=False, failed=failed)
    monkeypatch.setattr(service, 'confirm', forbidden)
    monkeypatch.setattr(service.program, 'verify', forbidden)
    monkeypatch.setattr(service.program, 'document', forbidden)
    result = startup.confirm(root)
    assert result['ready'] is True, 'a recorded cleanup failure does not block the start'
    assert result['action'] == 'none' and result['phase'] == 'confirmed'
    warning = result['warning']
    assert warning['code'] == 'upgrade_cleanup_incomplete'
    assert warning['failed'] == failed, 'the recorded reasons must survive the next start'
    assert '可以再次清理' in warning['message']
    assert '自动重试' not in warning['message'], 'a start never claims a retry it did not make'
    assert journal.load(root)['cleanup'] == record['cleanup'], 'a start never reruns or rewrites cleanup'


def test_a_rolled_back_upgrade_reports_no_cleanup_warning(root, local_acl, monkeypatch):
    """After a fallback the leftover cleanup is history: only a confirmed upgrade reports it."""
    failed = [{'generation': None, 'code': 'upgrade_preserve', 'reason': '保全备份校验失败'}]
    confirmed = recorded_cleanup(root, complete=False, failed=failed)
    ready = journal.advance(root, confirmed, 'rollback-ready',
                            rollback={'at': 'now', 'receipt': {'session': 's', 'at': 'now'},
                                      'preservation': artifact(root), 'preserved_locally': False,
                                      'settings': None, 'progress': None})
    rolling = journal.advance(root, ready, 'rolling-back',
                              rollback={**ready['rollback'],
                                        'progress': progress(ready['clone']['generation'])})
    record = journal.advance(root, rolling, 'rolled-back')
    assert record['cleanup']['complete'] is False, 'the record still carries the finished failure'
    monkeypatch.setattr(service, 'confirm', forbidden)
    monkeypatch.setattr(service.program, 'verify', forbidden)
    assert startup.confirm(root) == {'ready': True, 'action': 'none', 'phase': 'rolled-back',
                                     'warning': None}


def test_the_maintenance_verb_asks_the_startup_decision_for_this_root(root, monkeypatch):
    seen = {}

    def decide(root_value, *, vault=None):
        seen['root'] = root_value
        return {'ready': True, 'action': 'none', 'phase': None, 'warning': None}

    monkeypatch.setattr(startup, 'confirm', decide)
    monkeypatch.setattr(service, 'confirm', forbidden)
    args = argparse.Namespace(data_dir=str(root), app_dir='C:/user-supplied', upgrade_maintenance='startup-confirm')
    assert storage_worker.maintenance(service, args)['ready'] is True
    assert seen['root'] == str(root)


def test_the_worker_confirms_with_the_running_program_despite_a_command_line_directory(
        root, local_acl, monkeypatch, capsys, tmp_path):
    record_at(root, 'staged')
    running = tmp_path/'installed'
    seen = {}

    def confirm(root_value, app, *, vault=None):
        seen['app'] = app
        return {'ok': True, 'phase': 'confirmed', 'transaction': CLONE_TRANSACTION}

    monkeypatch.setattr(service, 'executing_app', lambda: running)
    monkeypatch.setattr(service, 'confirm', confirm)
    monkeypatch.setattr(sys, 'argv', ['worker', '--upgrade-maintenance', 'startup-confirm',
                                      '--data-dir', str(root), '--app-dir', 'C:/pretend-program'])
    assert storage_worker.main() == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer['ready'] is True and answer['phase'] == 'confirmed'
    assert seen['app'] == running, 'no command-line directory may stand in for the running program'


def test_the_startup_verb_needs_no_program_directory_but_the_others_still_do(root, monkeypatch,
                                                                             capsys):
    monkeypatch.setattr(startup, 'confirm',
                        lambda root_value: {'ready': True, 'action': 'none', 'phase': None})
    monkeypatch.setattr(sys, 'argv', ['worker', '--upgrade-maintenance', 'startup-confirm',
                                      '--data-dir', str(root)])
    assert storage_worker.main() == 0
    assert json.loads(capsys.readouterr().out)['ready'] is True
    monkeypatch.setattr(sys, 'argv', ['worker', '--upgrade-maintenance', 'confirm',
                                      '--data-dir', str(root)])
    with pytest.raises(SystemExit) as stopped:
        storage_worker.main()
    assert stopped.value.code == 2, 'confirm still requires the program directory it was given'


def test_a_refusal_reaches_the_host_as_a_stable_code_without_backend_text(root, local_acl,
                                                                         monkeypatch, capsys):
    record_at(root, 'prepared')
    monkeypatch.setattr(sys, 'argv', ['worker', '--upgrade-maintenance', 'startup-confirm',
                                      '--data-dir', str(root)])
    assert storage_worker.main() == 1
    assert json.loads(capsys.readouterr().out) == {'ok': False,
                                                   'error': 'upgrade_maintenance_in_progress'}
    assert journal.load(root)['phase'] == 'prepared'
