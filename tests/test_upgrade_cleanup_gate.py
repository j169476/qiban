"""Cleanup and abort behaviour on real files: what may be deleted, and what must survive.

Every case builds a real installation root, a real transaction record advanced through the real
journal API, and real generation trees under ``tmp_path``. Cleanup runs with the database layer
replaced by recorders, so the assertions are about decisions (which candidate is even considered,
what a missing or damaged own-preservation does, what the registry still holds after a failed
removal) plus the files that must survive on disk.

This module also provides the fixtures and helpers the independent review file imports.
"""
import contextlib
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))

import upgrade.cleanup as cleanup  # noqa: E402
import upgrade.data as data  # noqa: E402
import upgrade.journal as journal  # noqa: E402
import upgrade.registry as registry  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402

INSTALLATION = 'a'*32
OLD_PROGRAM = 'b'*64
NEW_PROGRAM = 'c'*64
OLD_GENERATION = 'd'*32
CLONE_TRANSACTION = 'e'*32
OLDER_TRANSACTION = 'f'*32
OTHER_GENERATION = '1234567890abcdef1234567890abcdef'


@pytest.fixture(autouse=True)
def local_acl(monkeypatch):
    """The ACL call needs Windows privileges the offline runner does not grant."""
    monkeypatch.setattr(journal, 'protect_directory', lambda path: None)


@pytest.fixture
def root(tmp_path):
    value = tmp_path/'Profile'
    (value/'store'/'history'/'portable-preservation').mkdir(parents=True)
    (value/'installation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'owner': 'o'*48, 'port': 54329,
         'phase': 'ready'}), encoding='utf8')
    return value


def digest(value):
    return hashlib.sha256(value).hexdigest()


def origin():
    """The fallback of this upgrade is a restored generation, not the original one."""
    return {'identity_sha256': OLD_PROGRAM,
            'selection': {'generation': OLD_GENERATION,
                          'database': 'qiban_restore_'+OLD_GENERATION,
                          'store': 'generations/'+OLD_GENERATION+'/store',
                          'credential_id': OLD_GENERATION, 'manifest_sha256': '3'*64},
            'data': {'fingerprint': '1'*64, 'migrations_sha256': '2'*64}}


def artifact(root, generation=OLD_GENERATION):
    content = b'CCBACKUP1:'+b'0'*32+b'\n'+b'ciphertext'
    folder = (root/'store' if generation is None
              else root/'generations'/generation/'store')/'history'/'portable-preservation'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'pre.ccbackup').write_bytes(content)
    return {'name': 'pre.ccbackup', 'sha256': digest(content), 'bytes': len(content),
            'generation': generation,
            'store': 'store' if generation is None else 'generations/'+generation+'/store'}


def record_at(root, phase):
    """One real record advanced through the real API to the requested phase."""
    transaction = CLONE_TRANSACTION
    record = journal.create(INSTALLATION, origin(), {'identity_sha256': NEW_PROGRAM},
                            {'identity_sha256': OLD_PROGRAM, 'count': 3, 'bytes': 30},
                            artifact(root), transaction=transaction)
    record = journal.save(root, record)
    if phase == 'prepared':
        return record
    plan = data.plan_clone(INSTALLATION, transaction)
    record = journal.advance(root, record, 'staging', plan=plan)
    if phase == 'staging':
        return record
    record = journal.advance(root, record, 'staged', clone={**plan, 'manifest_sha256': '5'*64})
    if phase == 'staged':
        return record
    if phase == 'rollback-ready':
        return journal.advance(root, record, 'rollback-ready',
                               rollback={'at': 'now', 'receipt': {'session': 's', 'at': 'now'},
                                         'preservation': artifact(root),
                                         'preserved_locally': False, 'settings': None,
                                         'progress': None})
    record = journal.advance(root, record, 'confirmed',
                             confirmed={'identity_sha256': NEW_PROGRAM, 'at': 'now',
                                        'data': {'fingerprint': '6'*64,
                                                 'migrations_sha256': '7'*64}})
    assert record['phase'] == 'confirmed'
    return record


def generation_tree(root, generation, *, working=True, unknown=False):
    """One generation directory with a ready manifest, a preservation copy and working assets."""
    directory = root/'generations'/generation
    store = directory/'store'
    (store/'history'/'portable-preservation').mkdir(parents=True, exist_ok=True)
    (store/'history'/'portable-preservation'/'keep.ccbackup').write_bytes(b'keep')
    (directory/'generation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'id': generation, 'ready': True}),
        encoding='utf8')
    if working:
        (store/'assets').mkdir()
        (store/'assets'/'a.png').write_bytes(b'asset')
        (store/'runtime').mkdir()
        (store/'runtime'/'write.lock').write_text('lock', encoding='utf8')
    if unknown:
        (store/'someone-elses').mkdir()
        (store/'someone-elses'/'x.bin').write_bytes(b'x')
    return directory


def manifest_digest(root, generation):
    raw = (root/'generations'/generation/'generation.json').read_bytes()
    return digest(raw)


class Vault:
    """A stand-in credential store: one data key per slot, no Windows contact."""

    def __init__(self, keys):
        self.keys = {INSTALLATION: b'1'*32, OLD_GENERATION: b'3'*32, **keys}

    def get_password(self, service, username):
        import base64

        key = self.keys.get(username)
        if key is None:
            return None
        return json.dumps({'data_key': base64.b64encode(key).decode('ascii'),
                           'database_password': 'r'*40, 'bootstrap_password': 'p'*40})


def candidate_receipt(root, generation, content=b'CCBACKUP1:'+b'1'*32+b'\n'+b'own'):
    folder = root/'generations'/generation/'store'/'history'/'portable-preservation'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'own.ccbackup').write_bytes(content)
    return {'name': 'own.ccbackup', 'sha256': digest(content), 'bytes': len(content)}


def registered(root, generation, *, transaction=OLDER_TRANSACTION, receipt=None, source='9'*64,
               manifest=None):
    plan = {'generation': generation, 'database': 'qiban_restore_'+generation,
            'store': 'generations/'+generation+'/store', 'credential_id': generation}
    return registry.register_generation(
        root, INSTALLATION, plan, transaction,
        manifest_sha256=manifest if manifest is not None else manifest_digest(root, generation),
        preservation=receipt, source_fingerprint=source if receipt else None)


@contextlib.contextmanager
def fake_admin(root, *, vault=None, app=None):
    yield {'root': root}


def run_clean(root, monkeypatch, *, vault=None, app=None):
    dropped = []
    monkeypatch.setattr(cleanup.data, 'admin', fake_admin)
    monkeypatch.setattr(cleanup.data, 'drop_database',
                        lambda session, database: dropped.append(database) or
                        {'database': database, 'dropped': True})
    report = cleanup.clean(root, state_value={'installation': INSTALLATION}, credential={},
                           vault=vault if vault is not None else Vault({}), app=app)
    return report, dropped


def test_a_candidate_without_its_own_receipt_is_a_failure_and_is_never_deleted(root, monkeypatch):
    """Nothing protects this generation and it cannot prove itself: a failure, not a normal skip."""
    record_at(root, 'confirmed')
    generation_tree(root, OTHER_GENERATION)
    registered(root, OTHER_GENERATION)
    report, dropped = run_clean(root, monkeypatch)
    assert dropped == []
    assert [entry['code'] for entry in report['cleanup']['failed']] == ['upgrade_preserve']
    assert report['cleanup']['skipped'] == [], 'only a still-protected generation is a normal skip'
    assert report['complete'] is False
    assert journal.load(root)['cleanup']['complete'] is False
    assert journal.load(root)['cleanup']['failed'] == report['cleanup']['failed']
    assert [entry['generation'] for entry in
            registry.load(root, INSTALLATION)['generations']] == [OTHER_GENERATION]


def test_a_damaged_receipt_blocks_the_deletion_and_keeps_the_entry(root, monkeypatch):
    """Regression: a damaged own preservation of an unprotected generation deletes nothing."""
    record_at(root, 'confirmed')
    generation_tree(root, OTHER_GENERATION)
    registered(root, OTHER_GENERATION, receipt=candidate_receipt(root, OTHER_GENERATION))
    seen = {}

    def corrupt(root_value, value, *, key):
        seen['name'] = value['name']
        raise AssistantError('upgrade_preserve', '保全备份校验失败')

    monkeypatch.setattr(cleanup.data, 'verify_artifact', corrupt)
    report, dropped = run_clean(root, monkeypatch,
                                vault=Vault({OTHER_GENERATION: b'2'*32}))
    assert seen['name'] == 'own.ccbackup'
    assert dropped == [], 'a failed own proof may not drop the database'
    assert [entry['code'] for entry in report['cleanup']['failed']] == ['upgrade_preserve']
    assert report['complete'] is False
    assert journal.load(root)['cleanup']['complete'] is False
    assert journal.load(root)['cleanup']['failed'] == report['cleanup']['failed']
    assert (root/'generations'/OTHER_GENERATION/'store'/'assets'/'a.png').is_file()
    assert OTHER_GENERATION in {entry['generation'] for entry in
                                registry.load(root, INSTALLATION)['generations']}


def test_a_verified_candidate_is_dropped_and_keeps_its_preservation_and_manifest(root, monkeypatch):
    record_at(root, 'confirmed')
    generation_tree(root, OTHER_GENERATION, unknown=True)
    registered(root, OTHER_GENERATION, receipt=candidate_receipt(root, OTHER_GENERATION))
    monkeypatch.setattr(cleanup.data, 'verify_artifact', lambda *args, **kwargs: None)
    report, dropped = run_clean(root, monkeypatch, vault=Vault({OTHER_GENERATION: b'2'*32}))
    assert dropped == ['qiban_restore_'+OTHER_GENERATION]
    removal = report['cleanup']['generations'][0]
    assert sorted(removal['removed']) == ['assets', 'runtime']
    assert any('someone-elses' in reason for reason in removal['skipped'])
    store = root/'generations'/OTHER_GENERATION/'store'
    assert (store/'history'/'portable-preservation'/'own.ccbackup').is_file()
    assert (root/'generations'/OTHER_GENERATION/'generation.json').is_file()
    assert not (store/'assets').exists()
    assert registry.load(root, INSTALLATION)['generations'] == []
    assert report['complete'] is True
    assert journal.load(root)['cleanup']['complete'] is True


def test_a_failed_removal_keeps_the_entry_and_reports_incomplete(root, monkeypatch):
    record_at(root, 'confirmed')
    generation_tree(root, OTHER_GENERATION)
    registered(root, OTHER_GENERATION, receipt=candidate_receipt(root, OTHER_GENERATION))
    monkeypatch.setattr(cleanup.data, 'verify_artifact', lambda *args, **kwargs: None)
    monkeypatch.setattr(cleanup.shutil, 'rmtree', lambda *args, **kwargs: None)
    report, dropped = run_clean(root, monkeypatch, vault=Vault({OTHER_GENERATION: b'2'*32}))
    assert dropped == ['qiban_restore_'+OTHER_GENERATION]
    assert report['complete'] is False
    assert journal.load(root)['cleanup']['complete'] is False
    assert journal.load(root)['cleanup']['failed'] == report['cleanup']['failed']
    assert [entry['code'] for entry in report['cleanup']['failed']] == ['upgrade_state']
    assert OTHER_GENERATION in {entry['generation'] for entry in
                                registry.load(root, INSTALLATION)['generations']}


def test_the_current_selection_and_the_rollback_target_are_never_candidates(root, monkeypatch):
    """Regression: keeping the current and the fallback generation is normal, not incomplete."""
    record_at(root, 'confirmed')
    generation_tree(root, OLD_GENERATION)
    generation_tree(root, OTHER_GENERATION)
    for generation in (OLD_GENERATION, OTHER_GENERATION):
        registered(root, generation, receipt=candidate_receipt(root, generation))
    monkeypatch.setattr(cleanup.data, 'selection_generation', lambda *args: OTHER_GENERATION)
    monkeypatch.setattr(cleanup.data, 'selected', lambda *args: {
        'generation': OTHER_GENERATION, 'database': 'qiban_restore_'+OTHER_GENERATION,
        'store': 'generations/'+OTHER_GENERATION+'/store', 'credential_id': OTHER_GENERATION,
        'manifest_sha256': '4'*64})
    report, dropped = run_clean(root, monkeypatch, vault=Vault({}))
    assert dropped == []
    reasons = [entry['reason'] for entry in report['cleanup']['skipped']]
    assert any('当前选择' in reason for reason in reasons)
    assert any('回退目标' in reason for reason in reasons)
    assert report['cleanup']['failed'] == []
    assert report['complete'] is True
    assert journal.load(root)['cleanup']['complete'] is True


def test_a_protected_generation_is_classified_before_its_own_proofs_are_taken(root, monkeypatch):
    """Protection is decided first: a kept generation that could not prove itself still skips."""
    record_at(root, 'confirmed')
    generation_tree(root, OLD_GENERATION)
    generation_tree(root, OTHER_GENERATION)
    for generation in (OLD_GENERATION, OTHER_GENERATION):
        registered(root, generation)
    monkeypatch.setattr(cleanup.data, 'selection_generation', lambda *args: OTHER_GENERATION)
    report, dropped = run_clean(root, monkeypatch, vault=Vault({}))
    assert dropped == []
    assert report['cleanup']['failed'] == [], 'a kept generation is not judged on its proofs'
    assert sorted(entry['generation'] for entry in report['cleanup']['skipped']) == sorted(
        [OLD_GENERATION, OTHER_GENERATION])
    assert report['complete'] is True


def test_a_changed_pointer_stops_the_deletion_that_was_already_approved(root, monkeypatch):
    record_at(root, 'confirmed')
    generation_tree(root, OTHER_GENERATION)
    registered(root, OTHER_GENERATION, receipt=candidate_receipt(root, OTHER_GENERATION))
    calls = {'n': 0}

    def move(*args):
        calls['n'] += 1
        return None if calls['n'] == 1 else OTHER_GENERATION

    monkeypatch.setattr(cleanup.data, 'selection_generation', move)
    monkeypatch.setattr(cleanup.data, 'verify_artifact', lambda *args, **kwargs: None)
    report, dropped = run_clean(root, monkeypatch, vault=Vault({OTHER_GENERATION: b'2'*32}))
    assert dropped == []
    assert [entry['code'] for entry in report['cleanup']['failed']] == ['upgrade_state']
    assert OTHER_GENERATION in {entry['generation'] for entry in
                                registry.load(root, INSTALLATION)['generations']}


def test_a_rolled_back_upgrade_never_triggers_automatic_cleanup(root):
    record = record_at(root, 'confirmed')
    ready = journal.advance(root, record, 'rollback-ready',
                            rollback={'at': 'now', 'receipt': None,
                                      'preservation': artifact(root),
                                      'preserved_locally': True, 'settings': None,
                                      'progress': None})
    rows = [{'path': 'Qiban.exe', 'bytes': 1, 'sha256': 'a'*64}]
    rolling = journal.advance(root, ready, 'rolling-back',
                              rollback={**ready['rollback'],
                                        'progress': {'at': 'now',
                                                     'pointer_before': record['clone']['generation'],
                                                     'pointer_switched': True,
                                                     'previous_files': rows, 'target_files': rows,
                                                     'previous_identity': OLD_PROGRAM,
                                                     'target_identity': NEW_PROGRAM}})
    journal.advance(root, rolling, 'rolled-back')
    with pytest.raises(AssistantError) as error:
        cleanup.clean(root, state_value={'installation': INSTALLATION}, credential={})
    assert error.value.code == 'upgrade_state'


def test_the_original_generation_is_a_candidate_with_its_own_receipt(root, monkeypatch):
    record = record_at(root, 'confirmed')
    receipt = artifact(root, None)
    (root/'store'/'assets').mkdir()
    (root/'store'/'assets'/'a.png').write_bytes(b'asset')
    (root/'store'/'index').mkdir()
    registry.register_generation(root, INSTALLATION,
                                 {'generation': None, 'database': 'qiban', 'store': 'store',
                                  'credential_id': INSTALLATION}, record['id'],
                                 preservation=receipt, source_fingerprint='8'*64)
    monkeypatch.setattr(cleanup.data, 'selection_generation', lambda *args: OLD_GENERATION)
    monkeypatch.setattr(cleanup.data, 'verify_artifact', lambda *args, **kwargs: None)
    report, dropped = run_clean(root, monkeypatch, vault=Vault({INSTALLATION: b'1'*32}))
    assert dropped == ['qiban'], 'the initial working database is not kept forever'
    assert not (root/'store'/'assets').exists()
    assert (root/'store'/'history'/'portable-preservation'/'pre.ccbackup').is_file()
    assert (root/'store'/'index').is_dir(), 'the index stays for the original generation'
    assert report['complete'] is True


def test_abort_cleans_an_interrupted_staging_so_the_upgrade_can_be_prepared_again(root, monkeypatch):
    import upgrade.service as service

    record = record_at(root, 'staging')
    plan = record['plan']
    directory = generation_tree(root, plan['generation'])
    registry.register_generation(root, INSTALLATION, plan, record['id'])
    monkeypatch.setattr(service.locks, 'exclusive', lambda *args, **kwargs: _nothing())
    monkeypatch.setattr(service, 'running', lambda: False)
    monkeypatch.setattr(service.data, 'selection_generation', lambda *args: OLD_GENERATION)
    monkeypatch.setattr(service.data, 'free_port', lambda *args: False)
    monkeypatch.setattr(service.data, 'admin', fake_admin)
    monkeypatch.setattr(service.data, 'drop_database', lambda *args: {'dropped': True})
    report = service.abort(root)
    assert report['phase'] == 'aborted'
    assert not directory.exists()
    assert registry.load(root, INSTALLATION)['generations'] == []
    assert report['cleaned']['directory_removed'] is True


@contextlib.contextmanager
def _nothing():
    yield


def test_abort_refuses_a_staging_attempt_that_already_moved_the_pointer(root, monkeypatch):
    import upgrade.service as service

    record = record_at(root, 'staging')
    monkeypatch.setattr(service.locks, 'exclusive', lambda *args, **kwargs: _nothing())
    monkeypatch.setattr(service.data, 'selection_generation',
                        lambda *args: record['plan']['generation'])
    with pytest.raises(AssistantError) as error:
        service.abort(root)
    assert error.value.code == 'upgrade_state'
