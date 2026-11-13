"""Offline checks for the UPGRADE-177 installer glue (``installer/prepare_upgrade.ps1``).

Nothing here touches an installed application, the real profile directory or the real Start Menu:
every fixture is a synthetic program directory, synthetic published manifests and a synthetic
frozen worker under ``tmp_path``, and the recovery shortcut path is injected. What is checked is
the glue's own decision surface - which of the four install shapes a given metadata set becomes,
which worker (installed program or recorded carrier) may be started, that a refusal never touches
an existing recovery shortcut, and that the forward clean-up of "old only" published files removes
exactly those files and nothing else.

The backend maintenance verbs, the upgrade journal and the native recovery window are NOT exercised
here; another batch owns them. The setup script itself is only checked statically, plus one
optional compile with the bundled Inno Setup compiler.
"""
import codecs
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT/'scripts'/'desktop_probe'/'installer'
GLUE = INSTALLER/'prepare_upgrade.ps1'
SETUP_SCRIPT = INSTALLER/'Qiban.iss'
ISCC = ROOT/'.dev'/'refactor-r0'/'desktop'/'installer-174'/'tools'/'inno'/'ISCC.exe'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
IDENTITY = 'qiban-payload.json'
WORKER_RELATIVE = 'resources/storage/Qiban.Storage.exe'
SERVICE_EXE = 'Qiban.exe'
DIGEST = 'a'*64
OTHER_DIGEST = 'c'*64


def _powershell():
    if sys.platform != 'win32':
        return None
    candidates = [Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'WindowsPowerShell'/
                  'v1.0'/'powershell.exe', Path(shutil.which('pwsh') or '')]
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return candidate
    return None


POWERSHELL = _powershell()

pytestmark = pytest.mark.skipif(POWERSHELL is None,
                                reason='installer glue needs the Windows PowerShell host')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def ps_quote(value):
    """One literal PowerShell string; the fixtures never contain a single quote today."""
    return "'" + str(value).replace("'", "''") + "'"


def manifest_bytes(files):
    """The published manifest of one program directory, in the installer's own layout."""
    rows = [{'path': name, 'bytes': len(data), 'sha256': sha256(data)}
            for name, data in sorted(files.items())]
    document = {'format': 1, 'count': len(rows), 'bytes': sum(row['bytes'] for row in rows),
                'files': rows}
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf8')


def write_program(directory, files):
    """A program directory with its own published identity; returns that identity's digest."""
    directory = Path(directory)
    for name, data in files.items():
        path = directory.joinpath(*name.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    document = manifest_bytes(files)
    (directory/IDENTITY).write_bytes(document)
    return sha256(document)


def program_files(storage=b'storage worker fixture\n'):
    return {SERVICE_EXE: b'MZ qiban shell fixture\n', 'README.txt': b'notes fixture\n',
            'desktop-build.json': b'{}\n', WORKER_RELATIVE: storage}


def write_transaction(root, *, phase, target, carrier, installation='b'*32, transaction='c'*32):
    """The one record the glue reads: format, phase, target identity and carrier identity."""
    value = {'format': 1, 'id': transaction, 'installation': installation, 'phase': phase,
             'steps': [phase], 'target': {'identity_sha256': target},
             'carrier': {'identity_sha256': carrier}}
    path = Path(root)/'upgrade'/'transaction.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='ascii')


def write_installation(root, phase='ready'):
    path = Path(root)/'installation.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'format': 1, 'installation': 'a'*32, 'owner': 'f'*48,
                                'port': 55432, 'phase': phase}), encoding='ascii')


def write_worker(path, log, *, exit_code=0, stdout='', transaction=None, transaction_path=None):
    """A synthetic frozen worker: it records how it was called, then answers like the real one.

    The fixture is a PowerShell script inside the program directory it belongs to. ``&`` on a .ps1
    runs it in the glue's own session, which is enough to observe the argument list and the exit
    code; one test below uses a real ``.cmd`` process instead to cover the native path.
    """
    lines = ['$ErrorActionPreference = "Continue"',
             'Add-Content -LiteralPath ' + ps_quote(log) + ' -Value (($args -join "|"))']
    if stdout:
        lines.append('Write-Output ' + ps_quote(stdout))
    if transaction is not None:
        lines.append('New-Item -ItemType Directory -Force -Path '
                     + ps_quote(Path(transaction_path).parent) + ' | Out-Null')
        lines.append('Set-Content -LiteralPath ' + ps_quote(transaction_path)
                     + ' -Value ' + ps_quote(transaction) + ' -Encoding ASCII')
    lines.append('exit ' + str(exit_code))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text('\n'.join(lines) + '\n', encoding='ascii')


def write_command_worker(path, log, exit_code):
    """A real native process that only answers with an exit code, like an argparse usage error."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(f'@echo off\r\necho %%*>>"{log}"\r\nexit /b {exit_code}\r\n',
                          encoding='ascii')


def worker_in(directory):
    """Where a synthetic worker for one program directory lives."""
    return Path(directory)/'resources'/'storage'/'worker-test.ps1'


def worker_arguments(log):
    return log.read_text(encoding='ascii').strip().split('|')


def read_result(path):
    values = {}
    path = Path(path)
    if not path.is_file():
        return values
    for line in path.read_text(encoding='ascii').splitlines():
        if '=' in line:
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
    return values


def run_glue(tmp_path, mode, *, app, root, target, manifest, link, worker=None):
    result = Path(tmp_path)/f'result-{mode}.txt'
    arguments = [str(POWERSHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                 '-File', str(GLUE), '-Mode', mode, '-AppDir', str(app),
                 '-InstallationRoot', str(root), '-TargetIdentity', target,
                 '-TargetManifestPath', str(manifest), '-RecoveryLinkPath', str(link),
                 '-ResultPath', str(result)]
    if worker is not None:
        arguments += ['-WorkerExecutable', str(worker)]
    finished = subprocess.run(arguments, capture_output=True, text=True, timeout=240,
                              creationflags=CREATE_NO_WINDOW)
    return finished.returncode, read_result(result), finished


def ansi_unrepresentable_character():
    """One character this machine's ANSI code page cannot express, or ``None``.

    The recorded way back lives in the Start Menu group of this application, and that group has a
    Chinese name. A Windows installation with an English code page - GitHub's runners among them -
    therefore has to write a shortcut path its ANSI code page cannot express, while a machine with
    a Chinese code page never sees that shape unless a fixture asks for it. The character is
    derived from the code page of the machine that runs the test, so the check is real everywhere.
    """
    if sys.platform != 'win32':
        return None
    try:
        code_page = f'cp{ctypes.windll.kernel32.GetACP()}'
        codecs.lookup(code_page)
    except (AttributeError, OSError, LookupError):
        return None
    for character in ('\u0e01', '\u6816', '\u05d0', '\u0905'):  # Thai, 栖, Hebrew, Devanagari
        try:
            if character.encode(code_page).decode(code_page) != character:
                return character
        except UnicodeEncodeError:
            return character
    return None


SHORTCUT_READER = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
 Guid("000214F9-0000-0000-C000-000000000046")]
public interface IReadShellLinkW
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszFile, int cch,
                 IntPtr pfd, uint fFlags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszName,
                        int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszDir,
                             int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszArgs,
                      int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    void GetHotkey(out short pwHotkey);
    void SetHotkey(short wHotkey);
    void GetShowCmd(out int piShowCmd);
    void SetShowCmd(int iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszIconPath,
                         int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
    void Resolve(IntPtr hwnd, uint fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
}

public static class RecoveryShortcutReader
{
    public static string[] Read(string linkPath)
    {
        object link = Activator.CreateInstance(Type.GetTypeFromCLSID(
            new Guid("00021401-0000-0000-C000-000000000046"), true));
        try
        {
            ((System.Runtime.InteropServices.ComTypes.IPersistFile)link).Load(linkPath, 0);
            var shellLink = (IReadShellLinkW)link;
            var path = new System.Text.StringBuilder(32768);
            var arguments = new System.Text.StringBuilder(32768);
            shellLink.GetPath(path, path.Capacity, IntPtr.Zero, 0);
            shellLink.GetArguments(arguments, arguments.Capacity);
            return new[] { path.ToString(), arguments.ToString() };
        }
        finally
        {
            Marshal.ReleaseComObject(link);
        }
    }
}
'@
$values = [RecoveryShortcutReader]::Read($args[0])
if ($values.Count -lt 2 -or $values[0] -eq '') { exit 3 }
[Console]::Out.WriteLine($values[0])
[Console]::Out.WriteLine($values[1])
'''


def read_shortcut(tmp_path, shortcut):
    """What one .lnk really contains, read through ``IShellLinkW`` into a caller-sized buffer.

    Both older readers hid a value from this check: ``WScript.Shell`` converts the shortcut path
    through the ANSI code page and reports nothing for the Chinese group a real installation uses,
    and the ``Shell.Application`` automation truncates ``Arguments`` at 259 characters
    (MAX_PATH - 1) although the file stores it in full. ``GetArguments`` fills the buffer the caller
    hands it, so the recorded value comes back whole at any length.
    """
    script = Path(tmp_path)/'read-shortcut.ps1'
    script.write_text(SHORTCUT_READER + '\n', encoding='ascii')
    finished = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive',
                               '-ExecutionPolicy', 'Bypass', '-File', str(script),
                               str(shortcut)],
                              capture_output=True, text=True, encoding='utf-8', timeout=120,
                              creationflags=CREATE_NO_WINDOW)
    lines = [line for line in finished.stdout.splitlines() if line.strip()]
    assert finished.returncode == 0, finished.stderr
    assert len(lines) >= 2, finished.stdout
    return lines[0].strip(), lines[1]


@pytest.fixture
def workspace(tmp_path):
    """An isolated profile, program directory and Start Menu directory."""
    space = tmp_path/'space'
    (space/'profile').mkdir(parents=True)
    (space/'programs').mkdir()
    link = space/'start-menu'/'栖伴'/'栖伴升级回退.lnk'
    return type('Workspace', (), {
        'tmp': Path(tmp_path), 'space': space,
        'root': space/'profile'/'data'/'installation',
        'app': space/'programs'/'Qiban',
        'link': link,
    })()


def carrier_of(space, files=None, root=None):
    """One recorded previous-build copy: its directory is named by its own published identity.

    The upgrade record carries that identity, and the glue checks the carrier's own
    ``qiban-payload.json`` against it, so a fixture may never use a made-up digest here.
    """
    programs = (Path(root) if root is not None
                else space/'profile'/'data'/'installation')/'upgrade'/'programs'
    staging = programs/'staging'
    digest = write_program(staging, files or program_files())
    carrier = programs/digest
    if carrier.exists():
        shutil.rmtree(carrier)
    staging.rename(carrier)
    return carrier, digest


# --------------------------------------------------------------------------------------------
# prepare: the four shapes
# --------------------------------------------------------------------------------------------

def test_identity_missing_is_refused(workspace):
    workspace.app.mkdir(parents=True)
    (workspace.app/WORKER_RELATIVE).parent.mkdir(parents=True)
    (workspace.app/WORKER_RELATIVE).write_bytes(b'worker\n')
    target = write_program(workspace.tmp/'target', program_files())
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'app_identity_missing'
    assert not workspace.link.exists()


def test_target_identity_must_match_the_embedded_manifest(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files())
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=OTHER_DIGEST, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'target_manifest_mismatch'
    assert target != OTHER_DIGEST


def test_repair_without_a_transaction_runs_no_worker(workspace):
    identity = write_program(workspace.app, program_files())
    log = workspace.tmp/'worker.log'
    write_worker(worker_in(workspace.app), log)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code == 0
    assert result['status'] == 'ready' and result['branch'] == 'repair'
    assert result['open'] == '0', 'nothing is open, so the forward clean-up has nothing to do'
    assert not log.exists(), 'a same-build repair must never start the maintenance worker'
    assert not workspace.link.exists()


def test_repair_keeps_the_recorded_way_back(workspace):
    identity = write_program(workspace.app, program_files())
    carrier, carrier_identity = carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='staged', target=identity, carrier=carrier_identity)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link)
    assert code == 0
    assert result['status'] == 'ready' and result['branch'] == 'repair'
    assert result['open'] == '1', 'a repair over a switched transaction finishes that upgrade'
    target, arguments = read_shortcut(workspace.tmp, workspace.link)
    assert Path(target) == carrier/SERVICE_EXE
    assert arguments == f'--upgrade-recovery "{workspace.root}" "{workspace.app}"'


def test_the_way_back_is_written_below_a_path_the_ansi_codepage_cannot_express(workspace):
    """The real entry sits in a Chinese Start Menu group, whatever code page the machine has.

    GitHub's Windows runners use an English code page (1252) that cannot express "栖伴", and a glue
    that wrote the entry through ``WScript.Shell`` failed there with ``recovery_link_failed`` while
    passing on a Chinese machine: a real upgrade would have been refused on every English Windows.
    The group name is derived from this machine's own code page, so the same shape is exercised on
    a machine whose code page does express the Chinese group name as well.
    """
    hostile = ansi_unrepresentable_character()
    if hostile is None:
        pytest.skip('this machine expresses every test character in its ANSI code page')
    identity = write_program(workspace.app, program_files())
    carrier, carrier_identity = carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='staged', target=identity, carrier=carrier_identity)
    link = workspace.space/'start-menu'/(hostile + 'group')/(hostile + 'upgrade-recovery.lnk')
    code, result, finished = run_glue(workspace.tmp, 'Prepare', app=workspace.app,
                                      root=workspace.root, target=identity,
                                      manifest=workspace.app/IDENTITY, link=link)
    assert code == 0, (result, finished.stderr)
    assert result['status'] == 'ready' and result['branch'] == 'repair'
    assert result['open'] == '1'
    target, arguments = read_shortcut(workspace.tmp, link)
    assert Path(target) == carrier/SERVICE_EXE
    assert arguments == f'--upgrade-recovery "{workspace.root}" "{workspace.app}"'


def test_the_recorded_arguments_are_not_capped_at_the_shell_string_limit(workspace):
    """The way back names both paths in full, whatever their length.

    The .lnk file stores the arguments well past MAX_PATH, but reading them back through the
    ``Shell.Application`` automation returns only their first 259 characters - a truncating reader
    is how a >260 character recovery command stayed unnoticed on a runner whose temporary paths are
    long. This fixture builds them that long on purpose and asserts the whole recorded value.
    """
    app = workspace.space/'programs'/('c' * 60)/'Qiban'
    identity = write_program(app, program_files())
    root = workspace.space/'profile'/'data'/'installation'
    carrier, carrier_identity = carrier_of(workspace.space, root=root)
    write_installation(root)
    write_transaction(root, phase='staged', target=identity, carrier=carrier_identity)
    expected = f'--upgrade-recovery "{root}" "{app}"'
    assert len(expected) > 260, f'the fixture has to pass the shell limit, got {len(expected)}'
    code, result, finished = run_glue(workspace.tmp, 'Prepare', app=app, root=root,
                                      target=identity, manifest=app/IDENTITY,
                                      link=workspace.link)
    assert code == 0, (result, finished.stderr)
    assert result['status'] == 'ready' and result['branch'] == 'repair'
    target, arguments = read_shortcut(workspace.tmp, workspace.link)
    assert Path(target) == carrier/SERVICE_EXE
    assert arguments == expected


def test_repair_with_a_transaction_for_another_target_is_refused(workspace):
    write_program(workspace.app, program_files())
    carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='staged', target=DIGEST, carrier=OTHER_DIGEST)
    workspace.link.parent.mkdir(parents=True)
    previous = b'previous valid entry bytes\n'
    workspace.link.write_bytes(previous)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=sha256((workspace.app/IDENTITY).read_bytes()),
                               manifest=workspace.app/IDENTITY, link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_target_mismatch'
    assert workspace.link.read_bytes() == previous, 'a refusal must keep the previous entry'


def test_repair_without_a_usable_carrier_is_refused(workspace):
    """A switched transaction whose recorded carrier is gone cannot be finished safely."""
    identity = write_program(workspace.app, program_files())
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='staged', target=identity, carrier=OTHER_DIGEST)
    workspace.link.parent.mkdir(parents=True)
    previous = b'previous valid entry bytes\n'
    workspace.link.write_bytes(previous)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_carrier_missing'
    assert workspace.link.read_bytes() == previous


@pytest.mark.parametrize('phase,expected', [('rollback-ready', 'upgrade_recovery_pending'),
                                            ('rolling-back', 'upgrade_recovery_pending'),
                                            ('emergency', 'upgrade_recovery_pending'),
                                            ('prepared', 'upgrade_pending'),
                                            ('staging', 'upgrade_pending')])
def test_repair_refuses_an_unfinished_transaction(workspace, phase, expected):
    identity = write_program(workspace.app, program_files())
    carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase=phase, target=identity, carrier=OTHER_DIGEST)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == expected


def test_cross_build_without_any_installation_replaces_the_program(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    log = workspace.tmp/'worker.log'
    write_worker(worker_in(workspace.app), log)
    workspace.link.parent.mkdir(parents=True)
    previous = b'entry from another install\n'
    workspace.link.write_bytes(previous)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code == 0
    assert result['status'] == 'ready' and result['branch'] == 'replace'
    assert not log.exists(), 'no pairing exists, so no maintenance worker may run'
    # A missing root proves nothing about the shared Start Menu entry: it is never removed here.
    assert workspace.link.read_bytes() == previous


def test_cross_build_with_a_partial_installation_is_not_treated_as_absent(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    write_installation(workspace.root, phase='planned')
    log = workspace.tmp/'worker.log'
    write_worker(worker_in(workspace.app), log)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'installation_not_ready'
    assert not log.exists()


def test_cross_build_prepares_with_the_installed_worker(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    _, carrier_identity = carrier_of(workspace.space)
    write_installation(workspace.root)
    log = workspace.tmp/'worker.log'
    record = json.dumps({'format': 1, 'id': 'd'*32, 'installation': 'a'*32, 'phase': 'staged',
                         'steps': ['staged'], 'target': {'identity_sha256': target},
                         'carrier': {'identity_sha256': carrier_identity}})
    write_worker(worker_in(workspace.app), log, stdout=json.dumps({'ok': True,
                                                                  'phase': 'staged'}),
                 transaction=record, transaction_path=workspace.root/'upgrade'/'transaction.json')
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code == 0
    assert result['status'] == 'ready' and result['branch'] == 'paired'
    assert result['carrier'] == carrier_identity
    calls = worker_arguments(log)
    assert calls[:2] == ['--upgrade-maintenance', 'prepare']
    assert calls[calls.index('--data-dir') + 1] == str(workspace.root)
    assert calls[calls.index('--app-dir') + 1] == str(workspace.app)
    assert calls[calls.index('--target-identity') + 1] == target
    target_path, arguments = read_shortcut(workspace.tmp, workspace.link)
    assert Path(target_path) == workspace.space/'profile'/'data'/'installation'/'upgrade'/'programs'/carrier_identity/SERVICE_EXE
    assert arguments == f'--upgrade-recovery "{workspace.root}" "{workspace.app}"'


def test_worker_without_the_maintenance_verb_is_a_refusal(workspace):
    """A 176-style worker does not know --upgrade-maintenance and exits with an argument error.

    This one is a real native process: the production worker is an .exe, and the setup must read a
    non-zero exit code without any JSON answer and refuse to touch the target directory.
    """
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    write_installation(workspace.root)
    log = workspace.tmp/'worker.log'
    worker = workspace.app/'resources'/'storage'/'worker-test.cmd'
    write_command_worker(worker, log, 2)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_worker_unsupported'
    assert log.exists(), 'the installed worker was asked once and refused'
    assert not workspace.link.exists()


def test_worker_answer_without_a_staged_record_is_refused(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    write_installation(workspace.root)
    log = workspace.tmp/'worker.log'
    write_worker(worker_in(workspace.app), log,
                 stdout=json.dumps({'ok': True, 'phase': 'prepared'}))
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_state'


def test_a_real_native_worker_process_can_prepare(workspace):
    """The production worker is an .exe: the same branch over a real child process, not a script."""
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    carrier, carrier_identity = carrier_of(workspace.space)
    write_installation(workspace.root)
    log = workspace.tmp/'worker.log'
    record = ('{"format":1,"id":"' + 'd'*32 + '","installation":"' + 'a'*32 + '","phase":"staged",'
              '"steps":["staged"],"target":{"identity_sha256":"' + target + '"},"carrier":'
              '{"identity_sha256":"' + carrier_identity + '"}}')
    worker = workspace.app/'resources'/'storage'/'worker-test.cmd'
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_text('@echo off\r\n'
                      f'echo %*>>"{log}"\r\n'
                      f'> "{workspace.root}/upgrade/transaction.json" '
                      f'echo {record}\r\n'
                      'echo {"ok": true, "phase": "staged"}\r\n'
                      'exit /b 0\r\n', encoding='ascii')
    (workspace.root/'upgrade').mkdir(parents=True, exist_ok=True)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker)
    assert code == 0, result
    assert result['status'] == 'ready' and result['branch'] == 'paired'
    calls = log.read_text(encoding='ascii').split()
    assert calls[:2] == ['--upgrade-maintenance', 'prepare']
    target_path, _ = read_shortcut(workspace.tmp, workspace.link)
    assert Path(target_path) == carrier/SERVICE_EXE


def test_backend_error_code_is_carried_through(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    write_installation(workspace.root)
    log = workspace.tmp/'worker.log'
    write_worker(worker_in(workspace.app), log, exit_code=1,
                 stdout=json.dumps({'ok': False, 'error': 'upgrade_pending'}))
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_pending'


def test_an_open_transaction_is_continued_by_the_carrier_worker_only(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    carrier, carrier_identity = carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='staged', target=target, carrier=carrier_identity)
    installed_log = workspace.tmp/'installed.log'
    carrier_log = workspace.tmp/'carrier.log'
    write_worker(worker_in(workspace.app), installed_log)
    write_worker(worker_in(carrier), carrier_log, stdout=json.dumps({'ok': True,
                                                                         'resumed': True}))
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(carrier))
    assert code == 0
    assert result['status'] == 'ready' and result['branch'] == 'resumed'
    assert not installed_log.exists(), 'the possibly mixed installed program must not run'
    calls = worker_arguments(carrier_log)
    assert calls[calls.index('--app-dir') + 1] == str(carrier)
    target_path, _ = read_shortcut(workspace.tmp, workspace.link)
    assert Path(target_path) == carrier/SERVICE_EXE


def test_carrier_branch_rejects_a_worker_outside_the_carrier(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    _, carrier_identity = carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='staged', target=target, carrier=carrier_identity)
    installed_log = workspace.tmp/'installed.log'
    write_worker(worker_in(workspace.app), installed_log)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_worker_missing'
    assert not installed_log.exists()


def test_an_open_transaction_for_another_target_runs_nothing(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    carrier, _ = carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='staged', target=DIGEST, carrier=OTHER_DIGEST)
    log = workspace.tmp/'carrier.log'
    write_worker(worker_in(carrier), log)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(carrier))
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_target_mismatch'
    assert not log.exists()


# --------------------------------------------------------------------------------------------
# finalize: only the previous build's own published files may go
# --------------------------------------------------------------------------------------------

def finalized_workspace(workspace, old_files, new_files, *, phase='staged'):
    """The state right after the file copy: the new build installed over the previous one.

    The previous build's own files that this package does not publish are still on disk (that is
    exactly what the forward clean-up looks at), the recorded previous-build copy carries the old
    complete manifest, and the package manifest is available as its own file.
    """
    target = write_program(workspace.app, new_files)
    published = {name.lower() for name in new_files}
    for name, data in old_files.items():
        if name.lower() in published:
            continue
        path = workspace.app.joinpath(*name.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    carrier, carrier_identity = carrier_of(workspace.space, old_files)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase=phase, target=target, carrier=carrier_identity)
    manifest = workspace.tmp/'package-manifest.json'
    manifest.write_bytes(manifest_bytes(new_files))
    return target, carrier, manifest


def test_finalize_removes_exactly_the_old_only_published_files(workspace):
    old = {'Qiban.exe': b'shell\n', 'README.txt': b'old notes\n', 'obsolete.dll': b'old only\n',
           'resources/storage/Qiban.Storage.exe': b'worker\n'}
    new = {'Qiban.exe': b'shell\n', 'README.txt': b'new notes\n',
           'resources/storage/Qiban.Storage.exe': b'worker\n'}
    target, _, manifest = finalized_workspace(workspace, old, new)
    unknown = workspace.app/'user-notes.txt'
    unknown.write_bytes(b'not ours\n')
    folder = workspace.app/'user-folder'
    folder.mkdir()
    (folder/'keep.txt').write_bytes(b'not ours\n')
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest,
                               link=workspace.link)
    assert code == 0
    assert result['status'] == 'ready' and result['branch'] == 'finalize'
    assert result['removed'] == '1'
    assert not (workspace.app/'obsolete.dll').exists()
    assert (workspace.app/'README.txt').read_bytes() == b'new notes\n'
    assert unknown.read_bytes() == b'not ours\n'
    assert (folder/'keep.txt').read_bytes() == b'not ours\n'


def test_finalize_is_idempotent(workspace):
    old = {'Qiban.exe': b'shell\n', 'obsolete.dll': b'old only\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, _, manifest = finalized_workspace(workspace, old, new)
    first = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                     target=target, manifest=manifest, link=workspace.link)
    second = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                      target=target, manifest=manifest, link=workspace.link)
    assert first[0] == 0 and first[1]['removed'] == '1'
    assert second[0] == 0 and second[1]['removed'] == '0'


def test_finalize_respects_case_differences(workspace):
    # Windows compares paths case-insensitively: the same published file under different letter
    # case is not "old only" and must survive.
    old = {'Qiban.exe': b'shell\n', 'Resources/Legacy.dll': b'shared\n'}
    new = {'Qiban.exe': b'shell\n', 'resources/legacy.dll': b'shared\n'}
    target, _, manifest = finalized_workspace(workspace, old, new)
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest, link=workspace.link)
    assert code == 0
    assert result['removed'] == '0'
    assert (workspace.app/'resources'/'legacy.dll').is_file()


def test_finalize_refuses_when_the_installed_identity_is_not_the_target(workspace):
    old = {'Qiban.exe': b'shell\n', 'obsolete.dll': b'old only\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, _, manifest = finalized_workspace(workspace, old, new)
    (workspace.app/'obsolete.dll').write_bytes(b'old only\n')
    (workspace.app/IDENTITY).write_bytes(b'{"tampered": true}')
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest,
                               link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'finalize_identity_mismatch'
    assert (workspace.app/'obsolete.dll').is_file(), 'nothing may be deleted on a refusal'


def test_finalize_refuses_after_a_transaction_for_another_target(workspace):
    old = {'Qiban.exe': b'shell\n', 'obsolete.dll': b'old only\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, carrier, manifest = finalized_workspace(workspace, old, new)
    write_transaction(workspace.root, phase='staged', target=DIGEST, carrier=OTHER_DIGEST)
    (workspace.app/'obsolete.dll').write_bytes(b'old only\n')
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest, link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_target_mismatch'
    assert (workspace.app/'obsolete.dll').is_file()
    assert carrier.is_dir()


def test_finalize_refuses_a_manifest_with_a_traversal_path(workspace):
    old = {'Qiban.exe': b'shell\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, carrier, manifest = finalized_workspace(workspace, old, new)
    document = {'format': 1, 'count': 2, 'bytes': 11,
                'files': [{'path': 'Qiban.exe', 'bytes': 6,
                           'sha256': sha256(b'shell\n')},
                          {'path': '../outside.txt', 'bytes': 5, 'sha256': sha256(b'evil\n')}]}
    (carrier/IDENTITY).write_text(json.dumps(document), encoding='ascii')
    (workspace.space/'profile'/'data'/'outside.txt').write_bytes(b'evil\n')
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest, link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse'
    assert (workspace.space/'profile'/'data'/'outside.txt').is_file()


def test_finalize_also_runs_after_the_upgrade_was_confirmed(workspace):
    """A copy that was interrupted before this step is finished by the next run, even after the
    new program already confirmed the pairing on its first start."""
    old = {'Qiban.exe': b'shell\n', 'obsolete.dll': b'old only\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, _, manifest = finalized_workspace(workspace, old, new, phase='confirmed')
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest, link=workspace.link)
    assert code == 0 and result['removed'] == '1'
    assert not (workspace.app/'obsolete.dll').exists()


def test_finalize_refuses_an_unrecognized_transaction_phase(workspace):
    old = {'Qiban.exe': b'shell\n', 'obsolete.dll': b'old only\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, _, manifest = finalized_workspace(workspace, old, new, phase='mystery-phase')
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest, link=workspace.link)
    assert code != 0 and result['status'] == 'refuse'
    assert (workspace.app/'obsolete.dll').is_file()


def test_a_repair_refuses_an_unrecognized_transaction_phase(workspace):
    identity = write_program(workspace.app, program_files())
    carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='mystery-phase', target=identity, carrier=OTHER_DIGEST)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_state'


def test_repair_after_a_finished_rollback_is_allowed(workspace):
    """A rolled-back record still names the build that is no longer installed: it constrains nothing."""
    identity = write_program(workspace.app, program_files())
    carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='rolled-back', target=DIGEST, carrier=OTHER_DIGEST)
    workspace.link.parent.mkdir(parents=True)
    previous = b'entry from that transaction\n'
    workspace.link.write_bytes(previous)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link)
    assert code == 0, result
    assert result['status'] == 'ready' and result['branch'] == 'repair'
    assert result['open'] == '0', 'a finished rollback never authorizes a forward clean-up'
    assert workspace.link.read_bytes() == previous


def test_repair_after_a_confirmed_transaction_still_cleans_up(workspace):
    """Confirming happened; a copy that stopped before the clean-up is finished by the next run."""
    identity = write_program(workspace.app, program_files())
    _, carrier_identity = carrier_of(workspace.space)
    write_installation(workspace.root)
    write_transaction(workspace.root, phase='confirmed', target=identity,
                      carrier=carrier_identity)
    workspace.link.parent.mkdir(parents=True)
    previous = b'existing recovery entry\n'
    workspace.link.write_bytes(previous)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link)
    assert code == 0, result
    assert result['status'] == 'ready' and result['branch'] == 'repair'
    assert result['open'] == '1', 'a confirmed same-target transaction still allows the clean-up'
    assert workspace.link.read_bytes() == previous, 'an existing entry is kept byte for byte'
    workspace.link.unlink()
    again = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                     target=identity, manifest=workspace.app/IDENTITY, link=workspace.link)
    assert again[0] == 0 and again[1]['open'] == '1'
    target_path, _ = read_shortcut(workspace.tmp, workspace.link)
    assert Path(target_path) == workspace.root/'upgrade'/'programs'/carrier_identity/SERVICE_EXE


def test_a_damaged_transaction_record_is_refused(workspace):
    """A file that exists but cannot be read is never read as "there is no transaction"."""
    identity = write_program(workspace.app, program_files())
    carrier_of(workspace.space)
    write_installation(workspace.root)
    (workspace.root/'upgrade').mkdir(parents=True, exist_ok=True)
    (workspace.root/'upgrade'/'transaction.json').write_bytes(b'{"format": 1, "phase":')
    workspace.link.parent.mkdir(parents=True)
    previous = b'previous valid entry bytes\n'
    workspace.link.write_bytes(previous)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=identity, manifest=workspace.app/IDENTITY,
                               link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'upgrade_journal_invalid'
    assert workspace.link.read_bytes() == previous


def test_installation_root_that_is_a_file_is_not_treated_as_absent(workspace):
    write_program(workspace.app, program_files())
    target = write_program(workspace.tmp/'target', program_files(b'new worker\n'))
    workspace.root.parent.mkdir(parents=True, exist_ok=True)
    workspace.root.write_bytes(b'not a directory\n')
    log = workspace.tmp/'worker.log'
    write_worker(worker_in(workspace.app), log)
    code, result, _ = run_glue(workspace.tmp, 'Prepare', app=workspace.app, root=workspace.root,
                               target=target, manifest=workspace.tmp/'target'/IDENTITY,
                               link=workspace.link, worker=worker_in(workspace.app))
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'installation_not_ready'
    assert not log.exists()


def test_finalize_refuses_a_carrier_manifest_that_is_not_the_recorded_one(workspace):
    old = {'Qiban.exe': b'shell\n', 'obsolete.dll': b'old only\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, carrier, manifest = finalized_workspace(workspace, old, new)
    # A well-formed manifest that is not the document the record names may not delete anything.
    (carrier/IDENTITY).write_bytes(manifest_bytes({'Qiban.exe': b'shell\n',
                                                  'obsolete.dll': b'old only\n',
                                                  'other.dll': b'x\n'}))
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest, link=workspace.link)
    assert code != 0
    assert result['status'] == 'refuse' and result['code'] == 'finalize_carrier_mismatch'
    assert (workspace.app/'obsolete.dll').is_file()


def test_finalize_never_deletes_through_a_junction(workspace):
    old = {'Qiban.exe': b'shell\n', 'linked/old.dll': b'old only\n'}
    new = {'Qiban.exe': b'shell\n'}
    target, _, manifest = finalized_workspace(workspace, old, new)
    outside = workspace.tmp/'outside'
    outside.mkdir()
    (outside/'old.dll').write_bytes(b'old only\n')
    outside_file = outside/'old.dll'
    junction = workspace.app/'linked'
    if junction.exists():
        shutil.rmtree(junction)
    made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                          capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if made.returncode != 0:
        pytest.skip('this machine cannot create a directory junction')
    code, result, _ = run_glue(workspace.tmp, 'Finalize', app=workspace.app, root=workspace.root,
                               target=target, manifest=manifest, link=workspace.link)
    assert code == 0, result
    assert result['removed'] == '0'
    assert result['skipped'] == '1'
    assert outside_file.read_bytes() == b'old only\n', 'nothing outside the app dir is touched'


# --------------------------------------------------------------------------------------------
# static wiring of the setup script and the glue itself
# --------------------------------------------------------------------------------------------

def test_glue_is_ascii_only():
    """Windows PowerShell 5.1 reads a script without a BOM as ANSI; the glue must stay ASCII."""
    raw = GLUE.read_bytes()
    assert b'\xef\xbb\xbf' not in raw
    raw.decode('ascii')


def test_setup_script_wires_the_glue_without_starting_the_gui():
    text = SETUP_SCRIPT.read_text(encoding='utf-8-sig')
    assert 'Source: "{#GlueScriptName}"; Flags: dontcopy' in text
    assert 'Source: "{#OutputDir}\\{#PayloadManifestName}"; Flags: dontcopy' in text
    assert "ExtractTemporaryFile(GlueScriptName);" in text
    assert "ExtractTemporaryFile(PayloadManifestName);" in text
    assert "-Mode ' + Mode" in text
    assert 'UpgradeFinalizeNeeded' in text and 'ssPostInstall' in text
    assert "ReadGlueValue('open')" in text, 'a repair over a switched transaction still finalizes'
    # The maintenance steps must never start the GUI: no Exec of Qiban.exe in [Code].
    assert "Exec(AddBackslash(Target) + AppExeName" not in text
    assert 'AppExeName, Parameters' not in text
    # The backend root is <profile>\data\installation. /DataRoot= names the profile and the
    # installation root below it is always derived, so no caller can pass a bare data directory.
    assert '#define InstallationSubPath "data\\installation"' in text
    assert "ExpandConstant('{param:DataRoot|}')" in text
    assert 'Result := AddBackslash(Profile) + InstallationSubPath;' in text
    # An explicit but unusable /DataRoot= stops the install; only an absent one uses the default.
    assert 'Given := ExpandConstant' in text and "if Given = '' then" in text
    # A missing installation root never proves the shared Start Menu entry is dead.
    assert 'Remove-StaleRecoveryLink' not in text
    # The recovery shortcut is the only thing the uninstaller deletes beyond its own log.
    delete_section = text.split('[UninstallDelete]', 1)[1].split('\n[', 1)[0]
    entries = [line for line in delete_section.splitlines()
               if line.strip().lower().startswith('type:')]
    assert len(entries) == 1 and '{#RecoveryLinkName}' in entries[0]


def test_builder_writes_the_setup_only_manifest(tmp_path):
    sys.path.insert(0, str(INSTALLER))
    import build_installer as installer
    document = b'{"format": 1}\n'
    name, digest = installer.write_identity(tmp_path, document)
    assert name == installer.IDENTITY
    assert digest == sha256(document)
    for expected in (installer.IDENTITY, installer.TARGET_MANIFEST):
        assert (tmp_path/expected).read_bytes() == document
    assert installer.TARGET_MANIFEST.endswith('.json')


@pytest.mark.skipif(not ISCC.is_file(), reason='bundled Inno Setup compiler is not present')
def test_setup_script_compiles_with_a_synthetic_payload(tmp_path):
    payload = tmp_path/'payload'
    payload.mkdir()
    (payload/'Qiban.exe').write_bytes(b'MZ fixture\n')
    (payload/'README.txt').write_bytes(b'fixture\n')
    identity = tmp_path/'qiban-payload.json'
    identity.write_bytes(manifest_bytes({'Qiban.exe': b'MZ fixture\n',
                                         'README.txt': b'fixture\n'}))
    output = tmp_path/'output'
    output.mkdir()
    shutil.copyfile(identity, output/'qiban-target-payload.json')
    digest = '1'*64
    finished = subprocess.run([
        str(ISCC), f'--define=PayloadDir={payload}', f'--define=OutputDir={output}',
        f'--define=PayloadIdentity={identity}', '--define=Version=0.9.4.177',
        f'--define=ExpectedBuildRecordSha256={digest}',
        f'--define=ExpectedPayloadIdentitySha256={digest}',
        '--no-ide-signtools', '--quiet-progress', str(SETUP_SCRIPT)],
        capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW)
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert (output/'Qiban-Setup-0.9.4.177.exe').is_file()
