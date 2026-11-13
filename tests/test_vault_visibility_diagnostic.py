"""A missing or failed child report must not make the vault diagnostic green."""
import copy
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'vault_visibility', Path(__file__).resolve().parents[1] / 'scripts/desktop_probe/check_vault_visibility.py')
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def complete():
    return {'reader_returncode': 0, 'cleanup': {'remaining': 0},
            'analysis': {'reader': dict.fromkeys(('expected', 'found', 'matching', 'distinct'), 16),
                         'lock_holds': 32, 'lock_overlaps': 0, 'stale_shared_reads': 0,
                         'missing_backups': 0, 'analysis_matches_reader': True},
            'children': [{'returncode': 0, 'error': None,
                          'readbacks': [{'found': True, 'match': True} for _ in range(4)]}
                         for _ in range(4)]}


def test_absent_reader_report_is_not_a_success():
    assert not diagnostic.every_slot_present({})
    assert not diagnostic.every_slot_present(dict.fromkeys(('expected', 'found', 'matching', 'distinct')))
    assert not diagnostic.every_slot_present(dict.fromkeys(('expected', 'found', 'matching', 'distinct'), 0))
    report = complete()
    assert diagnostic.round_passed(report)
    report['analysis']['reader'] = {}
    assert not diagnostic.round_passed(report)


def test_successful_read_does_not_hide_missing_writer_evidence_or_bad_exit():
    baseline = complete()
    for field, value in [('readbacks', None), ('readbacks', []), ('returncode', 1),
                         ('error', {'type': 'RuntimeError'})]:
        report = copy.deepcopy(baseline)
        report['children'][0][field] = value
        assert not diagnostic.round_passed(report)
    report = complete()
    report['children'][0]['readbacks'][0]['match'] = False
    assert not diagnostic.round_passed(report)


def test_missing_slot_overlap_or_cleanup_failure_stops_the_diagnostic():
    baseline = complete()
    for key in ('found', 'matching', 'distinct'):
        report = copy.deepcopy(baseline)
        report['analysis']['reader'][key] = 13
        assert not diagnostic.round_passed(report)
    for key in ('lock_overlaps', 'stale_shared_reads', 'missing_backups'):
        report = copy.deepcopy(baseline)
        report['analysis'][key] = 1
        assert not diagnostic.round_passed(report)
    report = complete()
    report['cleanup']['remaining'] = 1
    assert not diagnostic.round_passed(report)
