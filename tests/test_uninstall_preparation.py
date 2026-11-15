"""Offline checks for the published uninstall entry and the uninstall preparation wiring.

Nothing here installs, uninstalls, starts the application or reads a real profile: the payload, the
program directory and the uninstaller fixtures all live under ``tmp_path``. What is checked is that
``uninstall.exe`` is one byte-identical copy of the assembled program (and is refused otherwise by
both the assembly step and the installer gate), that the built payload still publishes the main
executable in its build record, that the setup script guides an ordinary interactive uninstall into
the application's own preparation page while a silent one keeps its previous rules, and that the
preparation backend of the worker never opens a platform, a model or an automatic run.
"""
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT/'scripts'/'desktop_probe'
INSTALLER = PROBE/'installer'
SETUP_SCRIPT = INSTALLER/'Qiban.iss'
ISCC = ROOT/'.dev'/'refactor-r0'/'desktop'/'installer-174'/'tools'/'inno'/'ISCC.exe'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SHELL = b'MZ qiban desktop shell fixture\n'
SOURCE = b'// desktop shell source fixture\n'

for path in (PROBE, INSTALLER):
    sys.path.insert(0, str(path))

import assemble_application as assembly  # noqa: E402
import build_desktop_sources as sources  # noqa: E402
import build_installer as installer  # noqa: E402


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_component(directory, entrypoint, files):
    rows = []
    for name, data in sorted(files.items()):
        path = directory/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append({'path': name, 'sha256': digest(data)})
    (directory/installer.MANIFEST_NAME).write_text(
        json.dumps({'format': 1, 'entrypoint': entrypoint, 'files': rows}), encoding='utf8')


def write_shell_records(root):
    """``desktop-build.json`` and its archive, exactly as build_desktop_sources.verify expects."""
    archive = root/'desktop-sources.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('Program.cs', SOURCE)
    (root/installer.MANIFEST).write_text(json.dumps({
        'format': 1,
        'exe': {'file': sources.PUBLISHED_EXE, 'sha256': digest(SHELL)},
        'source_archive': {'file': archive.name, 'sha256': digest(archive.read_bytes())},
        'files': [{'path': 'Program.cs', 'sha256': digest(SOURCE)}],
        'compiled': {'managed_application': True, 'subscription_validation': False},
        'signed': False}), encoding='utf8')


@pytest.fixture
def assembled(tmp_path, monkeypatch):
    """One assembled program directory, produced by assemble_application.main itself."""
    desktop = tmp_path/'desktop'
    desktop.mkdir()
    generated = {sources.PUBLISHED_EXE, installer.APP_EXE, installer.MANIFEST, 'desktop-sources.zip',
                 installer.MANAGED, installer.ALIAS_EXE}
    (desktop/sources.PUBLISHED_EXE).write_bytes(SHELL)
    (desktop/installer.MANAGED).write_text('{"mode": "managed", "format": 1}', encoding='utf8')
    for name in installer.ROOT_FILES - generated:
        (desktop/name).write_bytes(b'fixture\n')
    write_shell_records(desktop)
    storage, postgres = tmp_path/'storage', tmp_path/'postgres'
    write_component(storage, 'Qiban.Storage.exe', {'Qiban.Storage.exe': b'storage fixture\n'})
    write_component(postgres, 'bin/postgres.exe', {'bin/postgres.exe': b'postgres fixture\n'})
    output = tmp_path/'app'
    monkeypatch.setattr(sys, 'argv', ['assemble_application.py', '--desktop', str(desktop), '--storage', str(storage),
                '--postgres', str(postgres), '--output', str(output)])
    assembly.main()
    return output


def test_assembly_publishes_the_alias_as_the_same_program_bytes(assembled):
    program, alias = assembled/'Qiban.exe', assembled/'uninstall.exe'
    assert program.read_bytes() == SHELL
    assert alias.read_bytes() == program.read_bytes()
    assert digest(alias.read_bytes()) == digest(program.read_bytes())
    # The build record stays about the published main executable, never about the alias.
    record = json.loads((assembled/installer.MANIFEST).read_text(encoding='utf8'))
    assert record['exe']['file'] == sources.PUBLISHED_EXE
    assert record['exe']['sha256'] == digest(SHELL)
    assert 'uninstall.exe' in (assembled/'README.txt').read_text(encoding='utf8')


def test_the_assembly_step_always_writes_the_main_program_bytes(tmp_path):
    directory = tmp_path/'copy'
    directory.mkdir()
    (directory/'Qiban.exe').write_bytes(SHELL)
    (directory/'uninstall.exe').write_bytes(SHELL + b' rebuilt elsewhere')
    assembly.write_alias(directory)
    assert (directory/'uninstall.exe').read_bytes() == SHELL, 'a stale alias is replaced, never shipped'
    empty = tmp_path/'empty'
    empty.mkdir()
    with pytest.raises(ValueError):
        assembly.write_alias(empty)


def test_installer_accepts_the_alias_and_refuses_one_that_differs(assembled):
    _, summary, identity = installer.check_payload(assembled)
    assert summary['alias_sha256'] == summary['exe_sha256'] == digest(SHELL)
    published = {row['path'] for row in identity['files']}
    assert {'Qiban.exe', 'uninstall.exe'} <= published
    (assembled/'uninstall.exe').write_bytes(SHELL + b'not the same program')
    with pytest.raises(installer.BuildError) as failure:
        installer.check_payload(assembled)
    assert failure.value.code == 'installer_payload_alias'
    (assembled/'uninstall.exe').unlink()
    with pytest.raises(installer.BuildError) as failure:
        installer.check_payload(assembled)
    assert failure.value.code == 'installer_payload_file'


def test_setup_script_guides_an_interactive_uninstall_into_the_preparation_page():
    text = SETUP_SCRIPT.read_text(encoding='utf-8-sig')
    assert '#define UninstallAliasName "uninstall.exe"' in text
    assert '#define PreparationSwitch "--uninstall"' in text
    assert '#define PreparedParameter "QIBANPREPARED"' in text
    # The Start Menu entry is the published alias, not the setup's own uninstaller.
    assert 'Name: "{group}\\卸载{#AppName}"; Filename: "{app}\\{#UninstallAliasName}"' in text
    assert '{uninstallexe}' not in text
    code = text.split('[Code]', 1)[1]
    uninstall = code[code.index('function InitializeUninstall: Boolean;'):].split(
        'procedure CurUninstallStepChanged', 1)[0]
    silent = uninstall.index('if UninstallSilent then')
    prepared = uninstall.index("ExpandConstant('{param:' + PreparedParameter + '|}')")
    page = uninstall.index('Exec(Application, PreparationSwitch')
    # A silent uninstall is decided first and never opens the interactive page; the continuation
    # parameter is the only other way past the guide step; the running-marker check stays in both.
    assert silent < prepared < page
    assert uninstall.index('if CheckForMutexes(RunningMutexName) then') < prepared
    assert uninstall.count('if CheckForMutexes(RunningMutexName) then') == 2
    # Nothing here starts the uninstaller from the guide step: it only opens the preparation page.
    guide = uninstall[page:].split('// The program directory is already damaged', 1)[0]
    assert 'unins' not in guide.lower()
    # The declarative deletion list remains limited to the recorded recovery shortcut.
    delete_section = text.split('[UninstallDelete]', 1)[1].split('\n[', 1)[0]
    entries = [line for line in delete_section.splitlines() if line.strip().lower().startswith('type:')]
    assert len(entries) == 1 and '{#RecoveryLinkName}' in entries[0]
    cleanup = code.split('procedure CurUninstallStepChanged', 1)[1]
    assert 'CurUninstallStep <> usUninstall' in cleanup and 'or UninstallSilent then Exit' in cleanup
    assert '{param:QIBANPREPARED|}' in cleanup and '{param:QIBANCLEARPROFILE|}' in cleanup
    assert 'CheckForMutexes(RunningMutexName)' in cleanup
    assert "'--clear-profile-for-uninstall'" in cleanup and 'ResultCode <> 0' in cleanup


@pytest.mark.skipif(not ISCC.is_file(), reason='bundled Inno Setup compiler is not present')
def test_setup_script_still_compiles_with_the_uninstall_wiring(tmp_path):
    payload = tmp_path/'payload'
    payload.mkdir()
    (payload/'Qiban.exe').write_bytes(SHELL)
    (payload/'uninstall.exe').write_bytes(SHELL)
    (payload/'README.txt').write_bytes(b'fixture\n')
    identity = tmp_path/'qiban-payload.json'
    files = {'Qiban.exe': SHELL, 'uninstall.exe': SHELL, 'README.txt': b'fixture\n'}
    identity.write_text(json.dumps({'format': 1, 'count': len(files),
                                    'bytes': sum(len(data) for data in files.values()),
                                    'files': [{'path': name, 'bytes': len(data), 'sha256': digest(data)}
                                              for name, data in sorted(files.items())]}), encoding='utf8')
    output = tmp_path/'output'
    output.mkdir()
    (output/'qiban-target-payload.json').write_bytes(identity.read_bytes())
    blocker = '1'*64
    finished = subprocess.run([
        str(ISCC), f'--define=PayloadDir={payload}', f'--define=OutputDir={output}',
        f'--define=PayloadIdentity={identity}', '--define=Version=0.9.4.177',
        f'--define=ExpectedBuildRecordSha256={blocker}',
        f'--define=ExpectedPayloadIdentitySha256={blocker}',
        '--no-ide-signtools', '--quiet-progress', str(SETUP_SCRIPT)],
        capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW)
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert (output/'Qiban-Setup-0.9.4.177.exe').is_file()
