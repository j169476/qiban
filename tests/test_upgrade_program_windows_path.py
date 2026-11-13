"""Windows file I/O past MAX_PATH: the carrier copy, its proof and the way back.

The reported failure is a carrier whose own longest published path fitted (259 characters on the
customer installation) while the temporary directory ``copy_carrier`` builds beside it - the same
64-hex name plus ``.tmp-<8 hex>`` - did not, because that installation runs with
``LongPathsEnabled`` off. The old frozen worker answered ``upgrade_program_read`` and the upgrade
was over.

These cases run on real Windows paths longer than MAX_PATH and exercise only this module: a
published program directory at a short path is copied into a carrier at that length, proved,
written back over an installed directory of the newer build and proved again; a copy that fails
has to leave no temporary tree behind, however deep it already is. The ACL call is replaced the
way the other upgrade tests do it; no PostgreSQL, frozen payload or registry is involved.

A development machine usually has ``LongPathsEnabled`` turned on, where the plain form happens to
work as well, so a successful call proves nothing about the customer case by itself. Every file
operation of these tests is therefore watched: nothing past MAX_PATH may be handed to the OS
without the extended form, and at least one such path has to occur - otherwise the test is not
looking at the geometry it claims to.
"""
import builtins
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'/'desktop_probe'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import upgrade.journal as journal  # noqa: E402
import upgrade.program as program  # noqa: E402
from test_upgrade_program import NEW_FILES, OLD_FILES, published  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402

INSTALLATION = 'a'*32
TRANSACTION = 'b'*32
# The longest plain path the Win32 file APIs accept (the limit itself counts the terminator).
MAX_PATH = 259
# `copy_carrier` adds this to the 64-hex carrier name to name its temporary directory.
TEMPORARY_SUFFIX = len('.tmp-'+'0'*8)
WINDOWS = pytest.mark.skipif(os.name != 'nt', reason='the extended path form is a Windows rule')
REMOVED = 'resources/postgres/bin/pg_dump.exe'
EXTENDED = '\\\\?\\'
WATCHED = ('lstat', 'mkdir', 'makedirs', 'rename', 'replace', 'unlink', 'scandir')


@pytest.fixture(autouse=True)
def local_acl(monkeypatch):
    """The ACL call needs Windows privileges the offline runner does not grant."""
    monkeypatch.setattr(journal, 'protect_directory', lambda path: None)
    monkeypatch.setattr(program, 'protect_directory', lambda path: None)


def at(tmp_path, name, length):
    """A directory whose absolute path is exactly `length` characters."""
    stem = tmp_path/name
    pad = length-len(str(stem))-1
    assert 1 <= pad <= 200, f'the temporary directory of this run cannot be padded to {length}'
    path = stem/('d'*pad)
    path.mkdir(parents=True, exist_ok=True)
    assert len(str(path)) == length, (len(str(path)), length)
    return path


def write(directory, files):
    """Write one published file set, through the same path form the module itself uses."""
    for name, content in files.items():
        target = Path(directory)/name
        os.makedirs(program.native_io_path(target.parent), exist_ok=True)
        with open(program.native_io_path(target), 'wb') as stream:
            stream.write(content)


def watch_io(monkeypatch):
    """Watch every file operation of this test: a long path may only leave in the extended form."""
    state = {'long': 0, 'plain': []}

    def check(values):
        for value in values:
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            text = os.fspath(value)
            if not isinstance(text, str) or not os.path.isabs(text) or len(text) <= MAX_PATH:
                continue
            state['long'] += 1
            if not text.startswith(EXTENDED):
                state['plain'].append(text)

    def watched(module, name):
        real = getattr(module, name)

        def spy(*args, **kwargs):
            check(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, spy)

    watched(builtins, 'open')
    for name in WATCHED:
        watched(os, name)
    for name in ('isdir', 'isfile', 'exists'):
        watched(os.path, name)
    return state


def check_watched(state):
    """The test has to have seen the geometry, and must never have seen the plain form."""
    assert state['plain'] == [], 'a path past MAX_PATH was handed to the OS in the plain form'
    assert state['long'] > 0, 'no file operation of this call saw a path past MAX_PATH'


@WINDOWS
def test_native_io_path_is_the_extended_form_and_is_never_doubled(tmp_path):
    relative = program.native_io_path('some/relative/file.txt')
    assert relative.startswith(EXTENDED)
    assert relative[4:] == os.path.abspath('some/relative/file.txt'), \
        'a relative path was not made absolute'
    assert program.native_io_path(relative) == relative, 'an already prefixed path was doubled'
    assert program.native_io_path(Path(tmp_path)/'plain.txt') == EXTENDED+str(tmp_path/'plain.txt')
    share = program.native_io_path('\\\\server\\share\\file.txt')
    assert share == EXTENDED+'UNC\\server\\share\\file.txt'
    assert program.native_io_path(share) == share


@WINDOWS
def test_the_carrier_copy_proof_and_way_back_work_past_max_path(tmp_path, monkeypatch):
    """The whole published cycle, on the geometry the customer installation reported."""
    app = tmp_path/'app'
    app.mkdir()
    previous = published(app, OLD_FILES)
    new = tmp_path/'new'
    new.mkdir()
    current = published(new, NEW_FILES)

    root = at(tmp_path, 'root', 175)
    state = watch_io(monkeypatch)
    carrier = program.copy_carrier(root, app, previous, installation=INSTALLATION,
                                   transaction=TRANSACTION, roles=['carrier'])
    check_watched(state)
    assert len(str(carrier)) <= MAX_PATH, 'the carrier itself is meant to stay legal here'
    assert len(str(carrier))+TEMPORARY_SUFFIX > MAX_PATH, \
        'the temporary directory of this test was not past MAX_PATH'
    deepest = max(len(str(carrier.joinpath(*row['path'].split('/'))))
                  for row in previous['files'])
    assert deepest > MAX_PATH, 'the published files of the carrier are meant to be past MAX_PATH'
    program.verify(carrier, previous)
    assert program.carrier_record(carrier)['identity_sha256'] == previous['sha256']

    installed = at(tmp_path, 'installed', 235)
    write(installed, NEW_FILES)
    assert len(str(installed/'resources/storage/worker.exe')) > MAX_PATH

    restored = program.restore(carrier, installed, previous=previous, current=current)
    check_watched(state)
    assert restored['removed'] == [REMOVED]
    assert program.document(installed)['sha256'] == previous['sha256']
    program.verify(installed, previous)
    assert not os.path.exists(program.native_io_path(installed/REMOVED))
    for row in previous['files']:
        assert program.digest(installed.joinpath(*row['path'].split('/'))) == row['sha256']


@WINDOWS
def test_a_failed_copy_leaves_no_temporary_tree_behind(tmp_path, monkeypatch):
    """Removing that temporary tree has to work at the very length that made the copy fail."""
    app = tmp_path/'app'
    app.mkdir()
    previous = published(app, OLD_FILES)
    root = at(tmp_path, 'root', 175)
    real = program._copy
    deepest = 'resources/storage/worker.exe'

    def failing(source, destination, row):
        real(source, destination, row)
        if row['path'] == deepest:
            raise AssistantError('upgrade_program_read', '注入：复制后失败')

    state = watch_io(monkeypatch)
    monkeypatch.setattr(program, '_copy', failing)
    with pytest.raises(AssistantError) as error:
        program.copy_carrier(root, app, previous, installation=INSTALLATION,
                             transaction=TRANSACTION, roles=['carrier'])
    assert error.value.code == 'upgrade_program_read'
    check_watched(state)
    programs = Path(root)/'upgrade'/'programs'
    entries = sorted(entry.name for entry in os.scandir(program.native_io_path(programs)))
    assert not any('.tmp-' in name for name in entries), entries
    assert not os.path.exists(program.native_io_path(program.tree(root, previous['sha256'])))
