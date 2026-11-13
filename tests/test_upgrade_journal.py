"""The upgrade transaction record and its allocation registry, with no database involved.

Real files under ``tmp_path``: the record is written, advanced through its legal successors and
re-read, and a record that cannot be legal is refused instead of repaired. The registry cases
prove the part cleanup depends on: a generation entry carries its *own* preservation proof, the
original generation can be registered, and a foreign installation can never read this registry.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))

import upgrade.data as data  # noqa: E402
import upgrade.journal as journal  # noqa: E402
import upgrade.registry as registry  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402

INSTALLATION = 'a'*32
OLD = 'b'*64
NEW = 'c'*64
GENERATION = 'd'*32
TRANSACTION = 'e'*32
OTHER = 'f'*32


@pytest.fixture(autouse=True)
def local_acl(monkeypatch):
    """The ACL call needs Windows privileges the offline runner does not grant."""
    monkeypatch.setattr(journal, 'protect_directory', lambda path: None)


@pytest.fixture
def root(tmp_path):
    value = tmp_path/'Profile'
    value.mkdir()
    (value/'installation.json').write_text('{}', encoding='utf8')
    return value


def selection(generation=None):
    if generation is None:
        return {'generation': None, 'database': 'qiban', 'store': 'store',
                'credential_id': INSTALLATION, 'manifest_sha256': None}
    return {'generation': generation, 'database': 'qiban_restore_'+generation,
            'store': 'generations/'+generation+'/store', 'credential_id': generation,
            'manifest_sha256': '3'*64}


def origin(generation=None):
    return {'identity_sha256': OLD, 'selection': selection(generation),
            'data': {'fingerprint': '1'*64, 'migrations_sha256': '2'*64}}


def preservation(generation=None):
    return {'name': 'a'*32+'.ccbackup', 'sha256': '4'*64, 'bytes': 4096, 'generation': generation,
            'store': 'store' if generation is None else 'generations/'+generation+'/store'}


def carried():
    return {'identity_sha256': OLD, 'count': 7, 'bytes': 700}


def prepared(root, *, generation=None, transaction=TRANSACTION):
    return journal.save(root, journal.create(
        INSTALLATION, origin(generation), {'identity_sha256': NEW}, carried(),
        preservation(generation), transaction=transaction))


def staged(root, *, generation=None, transaction=TRANSACTION):
    record = prepared(root, generation=generation, transaction=transaction)
    plan = data.plan_clone(INSTALLATION, transaction)
    record = journal.advance(root, record, 'staging', plan=plan)
    return journal.advance(root, record, 'staged', clone={**plan, 'manifest_sha256': '5'*64})


def test_the_record_round_trips_and_refuses_a_stray_field(root):
    record = prepared(root)
    assert journal.load(root, required=True)['id'] == TRANSACTION
    with pytest.raises(AssistantError) as error:
        journal.save(root, {**record, 'surprise': 1})
    assert error.value.code == 'upgrade_journal_invalid'


def test_the_old_selection_may_be_any_recorded_generation(root):
    """A machine that was restored before must record the restored generation, not `None`."""
    saved = journal.save(root, journal.create(
        INSTALLATION, origin(GENERATION), {'identity_sha256': NEW}, carried(),
        preservation(GENERATION), transaction=TRANSACTION))
    assert saved['origin']['selection']['generation'] == GENERATION
    assert saved['origin']['selection']['database'] == 'qiban_restore_'+GENERATION
    assert saved['preservation']['generation'] == GENERATION


def test_a_selection_and_a_preservation_must_agree(root):
    with pytest.raises(AssistantError) as error:
        journal.create(INSTALLATION, origin(), {'identity_sha256': NEW}, carried(),
                       preservation(GENERATION))
    assert error.value.code == 'upgrade_journal_invalid'


def test_only_legal_successors_are_allowed(root):
    record = prepared(root)
    plan = data.plan_clone(INSTALLATION, TRANSACTION)
    with pytest.raises(AssistantError) as error:
        journal.advance(root, record, 'staged', clone={**plan, 'manifest_sha256': '5'*64})
    assert error.value.code == 'upgrade_state'
    record = journal.advance(root, record, 'staging', plan=plan)
    record = journal.advance(root, record, 'staged', clone={**plan, 'manifest_sha256': '5'*64})
    assert journal.load(root)['steps'] == ['prepared', 'staging', 'staged']


def test_a_phase_may_only_carry_the_parts_it_implies(root):
    record = staged(root)
    with pytest.raises(AssistantError):
        journal.save(root, {**record, 'clone': None})
    ready = journal.advance(root, record, 'rollback-ready',
                            rollback={'at': 'now', 'receipt': {'session': 's', 'at': 'now'},
                                      'preservation': preservation(),
                                      'preserved_locally': False, 'settings': None,
                                      'progress': None})
    assert ready['phase'] == 'rollback-ready'
    with pytest.raises(AssistantError) as error:
        journal.advance(root, ready, 'rolling-back')
    assert error.value.code == 'upgrade_journal_invalid'


def test_a_rolling_back_record_must_carry_both_complete_manifests(root):
    record = staged(root)
    ready = journal.advance(root, record, 'rollback-ready',
                            rollback={'at': 'now', 'receipt': {'session': 's', 'at': 'now'},
                                      'preservation': preservation(), 'preserved_locally': False,
                                      'settings': None, 'progress': None})
    rows = [{'path': 'Qiban.exe', 'bytes': 3, 'sha256': '6'*64}]
    progress = {'at': 'now', 'pointer_before': record['clone']['generation'],
                'pointer_switched': False, 'previous_files': rows, 'target_files': rows,
                'previous_identity': OLD, 'target_identity': NEW}
    rolling = journal.advance(root, ready, 'rolling-back',
                              rollback={**ready['rollback'], 'progress': progress})
    assert rolling['rollback']['progress']['previous_files'] == rows
    done = journal.advance(root, rolling, 'rolled-back',
                           rollback={**rolling['rollback'],
                                     'progress': {**progress, 'pointer_switched': True}})
    assert done['phase'] == 'rolled-back'
    escaped = {**rows[0], 'path': '../escape'}
    with pytest.raises(AssistantError):
        journal.advance(root, done, 'rolled-back',
                        rollback={**done['rollback'],
                                  'progress': {**progress, 'pointer_switched': True,
                                               'previous_files': [escaped]}})


def test_a_clone_id_is_derived_from_the_transaction(root):
    first = data.plan_clone(INSTALLATION, TRANSACTION)
    assert first == data.plan_clone(INSTALLATION, TRANSACTION)
    assert first['generation'] != data.plan_clone(INSTALLATION, OTHER)['generation']
    assert first['database'] == 'qiban_restore_'+first['generation']
    assert first['credential_id'] == first['generation']
    with pytest.raises(AssistantError):
        data.plan_clone(INSTALLATION, 'not-a-transaction')


def test_the_registry_registers_a_generation_with_its_own_receipt(root):
    plan = data.plan_clone(INSTALLATION, TRANSACTION)
    artifact = {'name': 'x'*32+'.ccbackup', 'sha256': '7'*64, 'bytes': 2048}
    value = registry.register_generation(root, INSTALLATION, plan, TRANSACTION,
                                         manifest_sha256='8'*64, preservation=artifact,
                                         source_fingerprint='9'*64)
    entry = value['generations'][0]
    assert entry['generation'] == plan['generation']
    assert entry['preservation'] == artifact
    assert entry['preservation_keyref'] == entry['credential_id'], 'the slot that decrypts it'
    assert entry['source_fingerprint'] == '9'*64 and entry['receipt_at']
    again = registry.register_generation(root, INSTALLATION, plan, TRANSACTION)
    assert again == value, 'registering the same generation twice is a no-op'


def test_the_original_generation_can_be_registered_and_needs_a_slot_of_its_own(root):
    artifact = {'name': 'y'*32+'.ccbackup', 'sha256': 'a'*64, 'bytes': 1024}
    value = registry.register_generation(root, INSTALLATION, selection(), TRANSACTION,
                                         preservation=artifact, source_fingerprint='b'*64)
    entry = value['generations'][0]
    assert entry['generation'] is None and entry['database'] == 'qiban'
    assert entry['store'] == 'store' and entry['credential_id'] == INSTALLATION
    assert entry['preservation_keyref'] == INSTALLATION


def test_a_receipt_is_only_accepted_with_its_slot_and_fingerprint(root):
    plan = data.plan_clone(INSTALLATION, TRANSACTION)
    entry = registry.register_generation(root, INSTALLATION, plan, TRANSACTION)
    assert entry['generations'][0]['preservation'] is None
    broken = {**entry, 'generations': [{**entry['generations'][0],
                                       'preservation': {'name': 'z'*32+'.ccbackup',
                                                        'sha256': 'c'*64, 'bytes': 10}}]}
    with pytest.raises(AssistantError) as error:
        registry.validate(broken, INSTALLATION)
    assert error.value.code == 'upgrade_registry_invalid', 'a receipt without its slot is invalid'
    wrong_slot = {**entry, 'generations': [{**entry['generations'][0],
                                           'preservation': {'name': 'z'*32+'.ccbackup',
                                                            'sha256': 'c'*64, 'bytes': 10},
                                           'preservation_keyref': OTHER,
                                           'source_fingerprint': 'd'*64,
                                           'receipt_at': 'now'}]}
    with pytest.raises(AssistantError) as error:
        registry.validate(wrong_slot, INSTALLATION)
    assert error.value.code == 'upgrade_registry_invalid', 'another slot cannot own this receipt'


def test_program_roles_union_and_references_come_from_the_record(root):
    published = {'sha256': OLD, 'count': 7, 'bytes': 700}
    registry.register_program(root, INSTALLATION, published, TRANSACTION, 'carrier')
    value = registry.register_program(root, INSTALLATION, published, TRANSACTION, 'previous')
    assert value['programs'][0]['roles'] == ['carrier', 'previous']
    assert registry.drop_program(root, INSTALLATION, OLD, 'previous')['programs'][0]['roles'] == ['carrier']
    record = staged(root)
    generations, programs = registry.referenced(record)
    assert record['clone']['generation'] in generations
    assert record['origin']['selection']['generation'] in generations
    assert {OLD, NEW} <= programs


def test_a_foreign_installation_cannot_read_this_registry(root):
    registry.register_generation(root, INSTALLATION, data.plan_clone(INSTALLATION, TRANSACTION),
                                 TRANSACTION)
    with pytest.raises(AssistantError) as error:
        registry.load(root, '9'*32)
    assert error.value.code == 'upgrade_registry_invalid'
