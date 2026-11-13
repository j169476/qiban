"""Published identities, the maintenance carrier and exact-set program restore.

Every case builds real directories under ``tmp_path``: a synthetic published program with its
``qiban-payload.json``, the carrier copy, and a program directory that has been "overwritten" by
a newer build. The rollback assertions are about bytes on disk - which files are rewritten, which
one newer-only file is removed, and which unrelated file must stay exactly as it was.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))

import upgrade.journal as journal  # noqa: E402
import upgrade.program as program  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402

INSTALLATION = 'a'*32
TRANSACTION = 'b'*32
OLD_FILES = {'Qiban.exe': b'MZ old shell\n', 'README.txt': b'old readme\n',
             'resources/storage/worker.exe': b'old worker\n'}
NEW_FILES = {'Qiban.exe': b'MZ new shell\n', 'README.txt': b'new readme\n',
             'resources/storage/worker.exe': b'new worker\n',
             'resources/postgres/bin/pg_dump.exe': b'new tool\n'}


@pytest.fixture(autouse=True)
def local_acl(monkeypatch):
    monkeypatch.setattr(journal, 'protect_directory', lambda path: None)
    monkeypatch.setattr(program, 'protect_directory', lambda path: None)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def published(directory, files):
    """One synthetic published program directory with a deterministic identity document."""
    rows = []
    for name, content in sorted(files.items()):
        target = directory/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        rows.append({'path': name, 'bytes': len(content), 'sha256': digest(content)})
    value = {'format': 1, 'count': len(rows), 'bytes': sum(row['bytes'] for row in rows),
             'files': rows}
    (directory/'qiban-payload.json').write_bytes(
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf8'))
    return program.document(directory)


@pytest.fixture
def root(tmp_path):
    value = tmp_path/'Profile'
    value.mkdir()
    (value/'installation.json').write_text('{}', encoding='utf8')
    return value


@pytest.fixture
def installed(tmp_path):
    directory = tmp_path/'app'
    directory.mkdir()
    return directory, published(directory, OLD_FILES)


def test_the_published_identity_is_read_and_validated(installed):
    directory, identity = installed
    assert identity['count'] == 3 and identity['bytes'] == sum(
        len(content) for content in OLD_FILES.values())
    assert identity['sha256'] == digest((directory/'qiban-payload.json').read_bytes())
    assert [row['path'] for row in identity['files']] == sorted(OLD_FILES)


def test_a_malformed_identity_is_refused(tmp_path):
    directory = tmp_path/'broken'
    directory.mkdir()
    (directory/'qiban-payload.json').write_text(json.dumps({'format': 1, 'count': 1}), encoding='utf8')
    with pytest.raises(AssistantError) as error:
        program.document(directory)
    assert error.value.code == 'upgrade_program_invalid'
    unsorted = tmp_path/'escaping'
    unsorted.mkdir()
    (unsorted/'qiban-payload.json').write_text(json.dumps(
        {'format': 1, 'count': 1, 'bytes': 4,
         'files': [{'path': '../escape', 'bytes': 4, 'sha256': 'a'*64}]}), encoding='utf8')
    with pytest.raises(AssistantError) as error:
        program.document(unsorted)
    assert error.value.code == 'upgrade_program_invalid'


def test_the_carrier_is_copied_proved_and_marked(root, installed):
    directory, identity = installed
    carrier = program.copy_carrier(root, directory, identity, installation=INSTALLATION,
                                   transaction=TRANSACTION, roles=['carrier'])
    assert carrier == program.tree(root, identity['sha256'])
    mark = program.carrier_record(carrier)
    assert mark['installation'] == INSTALLATION and mark['transaction'] == TRANSACTION
    assert mark['roles'] == ['carrier']
    for row in identity['files']:
        assert digest((carrier/row['path']).read_bytes()) == row['sha256']
    assert digest((carrier/'qiban-payload.json').read_bytes()) == identity['sha256']


def test_a_tampered_carrier_file_is_refused(root, installed):
    directory, identity = installed
    carrier = program.copy_carrier(root, directory, identity, installation=INSTALLATION,
                                   transaction=TRANSACTION, roles=['carrier'])
    (carrier/'README.txt').write_bytes(b'tampered\n')
    with pytest.raises(AssistantError) as error:
        program.verify(carrier, identity)
    assert error.value.code == 'upgrade_program_changed'


def test_a_tampered_source_file_never_becomes_a_carrier(root, installed):
    directory, identity = installed
    (directory/'README.txt').write_bytes(b'edited after the record was written\n')
    with pytest.raises(AssistantError) as error:
        program.copy_carrier(root, directory, identity, installation=INSTALLATION,
                             transaction=TRANSACTION, roles=['carrier'])
    assert error.value.code == 'upgrade_program_changed'
    assert not program.tree(root, identity['sha256']).exists()


def test_the_rollback_removes_only_the_newer_builds_extra_files(root, installed, tmp_path):
    directory, previous = installed
    carrier = program.copy_carrier(root, directory, previous, installation=INSTALLATION,
                                   transaction=TRANSACTION, roles=['carrier'])
    current = published(directory, NEW_FILES)
    (directory/'unins000.exe').write_bytes(b'installer owned\n')
    restored = program.restore(carrier, directory, previous=previous, current=current)
    assert restored['removed'] == ['resources/postgres/bin/pg_dump.exe']
    assert restored['identity_sha256'] == previous['sha256']
    assert not (directory/'resources/postgres/bin/pg_dump.exe').exists()
    for row in previous['files']:
        assert digest((directory/row['path']).read_bytes()) == row['sha256']
    assert (directory/'unins000.exe').read_bytes() == b'installer owned\n', 'a file in neither record stays'
    assert program.document(directory)['sha256'] == previous['sha256']


def test_a_rollback_plan_is_exactly_the_two_verified_documents(root, installed):
    directory, previous = installed
    current = published(directory, NEW_FILES)
    plan = program.plan_restore(previous, current)
    assert [row['path'] for row in plan['write']] == sorted(OLD_FILES)
    assert plan['remove'] == ['resources/postgres/bin/pg_dump.exe']
    assert plan['keep'] == sorted(OLD_FILES)


def test_a_rollback_refuses_a_carrier_that_no_longer_matches(root, installed):
    directory, previous = installed
    carrier = program.copy_carrier(root, directory, previous, installation=INSTALLATION,
                                   transaction=TRANSACTION, roles=['carrier'])
    current = published(directory, NEW_FILES)
    (carrier/'Qiban.exe').write_bytes(b'MZ something else\n')
    with pytest.raises(AssistantError) as error:
        program.restore(carrier, directory, previous=previous, current=current)
    assert error.value.code == 'upgrade_program_changed'
    assert program.document(directory)['sha256'] == current['sha256'], 'the program was not rewritten'
