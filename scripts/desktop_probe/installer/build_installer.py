"""Compile the per-user Qiban installer from one already assembled application directory.

This tool does not build the desktop shell, the storage worker, PostgreSQL or any model resource:
``build.ps1`` + ``assemble_application.py`` already produced that directory, and this tool only
proves it is still exactly the assembled application before handing it to the official Inno Setup
compiler unchanged.

Compile-time inputs are checked before anything is written, and nothing here is a fallback hash
scheme of its own:

* the payload root file set must equal this release's exact published list - ``Qiban.exe``,
  ``README.txt``, ``managed-application.json``, ``desktop-build.json``, ``desktop-sources.zip`` and
  the eight shipped license/notice files. The only root directory is ``resources``, whose children
  may only be ``storage``, ``postgres``, ``speech``, ``wechat``, ``qq``;
* ``build_desktop_sources.verify`` re-proves that the executable, its sources archive and its
  build record still belong to each other;
* ``assemble_application.validate`` re-proves every component manifest digest, and each component
  tree must contain exactly its ``runtime-manifest.json`` plus the files that manifest lists - an
  unregistered file (a copied profile, a stray log, a hand-added binary) is refused;
* ``companion_qq.native_resources.verify_runtime`` re-proves the QQ native runtime;
* every directory and file is refused when it is a symbolic link, junction or other reparse point,
  so the walk can never leave the payload and pack something else;
* the digest of the whole ``desktop-build.json`` becomes the Inno macro that lets the installer
  recognise the same build later for a repair, and refuse any other build;
* one deterministic ``qiban-payload.json`` lists every published file with its byte count and
  SHA-256 - relative paths only, no clock, no version, no source path - and the digest of that
  document is the build identity the installer compares before it writes anything. It is written
  next to the setup and installed by the script; the assembled payload is never modified. The
  comparison reads that one record and never re-hashes the target's files, because a damaged or
  missing payload file is exactly what a same-build repair has to be able to overwrite.

The payload identity covers every published file, so a build that changes only one component -
for example a rebuilt ``resources/postgres`` with an unchanged executable and shell record - is a
different build and is refused instead of being repaired over the other one.

The package does not implement an upgrade transaction itself: pairing a new program with existing
data is done by the *installed* build's own frozen maintenance worker, asked for it by the setup's
embedded glue (``prepare_upgrade.ps1``), and only a build that reports that it can prepare one is
ever upgraded. A build without the maintenance verb is refused before a single file is written;
the same-build repair, a first install and a replacement of a program whose default profile never
created an installation stay plain file copies. An uninstaller only removes the files and
shortcuts it recorded, plus the one upgrade-recovery shortcut the glue creates, and personal data
outside the program directory is never touched. The identity identifies which published file set
is installed; it is not a publisher signature, it is not claimed to resist deliberate local
tampering, and it pairs no program with any data on its own. Non-zero exit means no setup and no
build record were produced; a validated ``qiban-payload.json`` may stay in the output directory to
show what the compile failed on.

Two deterministic manifests are written beside the setup: ``qiban-payload.json`` (installed into
the program directory and compared by the gate) and ``qiban-target-payload.json``, byte-identical
under a setup-only name that the setup embeds without installing, so the maintenance glue always
receives this package's own published file list as one unambiguous path.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE.parent
REPO = PROBE.parent.parent
SCRIPT = HERE/'Qiban.iss'
NOTES = HERE/'INSTALL-NOTES.txt'
GLUE = HERE/'prepare_upgrade.ps1'
DEFAULT_OUTPUT = REPO/'.dev/refactor-r0/desktop/installer-174/output'
DEFAULT_ISCC = REPO/'.dev/refactor-r0/desktop/installer-174/tools/inno/ISCC.exe'
DEFAULT_VERSION = '0.9.4.174'
RECORD = 'installer-build.json'
IDENTITY = 'qiban-payload.json'
# Byte-identical copy of IDENTITY under a setup-only name. The setup embeds this one (dontcopy) so
# the maintenance glue can be handed ``-TargetManifestPath`` without guessing which of two identical
# [Files] entries to extract. It is never installed and never compared directly by the gate.
TARGET_MANIFEST = 'qiban-target-payload.json'
IDENTITY_FORMAT = 1
FORMAT = 1
APP_EXE = 'Qiban.exe'
MANAGED = 'managed-application.json'
MANIFEST_NAME = 'runtime-manifest.json'
MANAGED_RECORD = {'mode': 'managed', 'format': 1}
# Exactly the files one assembled application directory publishes at its root.
ROOT_FILES = frozenset({
    'CsWinRT-LICENSE.txt', 'Microsoft.Web.WebView2.Core.xml', 'Microsoft.Web.WebView2.WinForms.xml',
    'Qiban-LICENSE.txt', 'Qiban.exe', 'README.txt', 'WebView2-SDK.txt', 'Windows-SDK.rtf',
    'desktop-build.json', 'desktop-sources.zip', 'dotnet-desktop-LICENSE.txt',
    'dotnet-runtime-LICENSE.txt', 'dotnet-runtime-THIRD-PARTY-NOTICES.txt',
    'managed-application.json'})
ROOT_DIRECTORIES = frozenset({'resources'})
RESOURCE_NAMES = frozenset({'storage', 'postgres', 'speech', 'wechat', 'qq'})
REQUIRED_RESOURCES = ('storage', 'postgres')
MANIFEST_RESOURCES = (('storage', 'Qiban.Storage.exe'), ('postgres', 'bin/postgres.exe'),
                      ('speech', 'Qiban.Speech.exe'), ('wechat', 'Qiban.WeChatReader.exe'))
VERSION_PATTERN = re.compile(r'\d+(\.\d+){1,3}')
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
sys.path.insert(0, str(PROBE))
sys.path.insert(0, str(REPO/'src'))
from assemble_application import validate as validate_resources  # noqa: E402
from build_desktop_sources import MANIFEST, BuildError  # noqa: E402
from build_desktop_sources import verify as verify_desktop_build  # noqa: E402


def _digest(path):
    number = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            number.update(block)
    return number.hexdigest()


def payload_identity(payload, names):
    """Every published file as ``path``/``bytes``/``sha256``, sorted by its relative path.

    The document holds no absolute path, no clock and no version: it describes the published file
    set itself, so the same payload always produces the same bytes. The installer's own metadata
    file is never part of it - it is written beside the setup, not into the payload.
    """
    rows = []
    for name in sorted(names):
        path = Path(payload)/name
        try:
            rows.append({'path': name, 'bytes': path.stat().st_size, 'sha256': _digest(path)})
        except OSError:
            raise BuildError('installer_payload_read', f'应用目录文件无法读取：{name}') from None
    return {'format': IDENTITY_FORMAT, 'count': len(rows),
            'bytes': sum(row['bytes'] for row in rows), 'files': rows}


def identity_document(identity):
    """Deterministic UTF-8 JSON of one payload identity; identical input gives identical bytes."""
    return (json.dumps(identity, ensure_ascii=False, sort_keys=True, indent=2,
                       separators=(',', ': ')) + '\n').encode('utf8')


def write_identity(output, document):
    """Write both manifests beside the setup and return the identity file name and digest.

    ``qiban-payload.json`` is the identity the gate compares inside the program directory;
    ``qiban-target-payload.json`` carries exactly the same bytes under a name that only the setup
    embeds, so the maintenance glue is always handed this package's own published list.
    """
    digest = hashlib.sha256(document).hexdigest()
    for name in (IDENTITY, TARGET_MANIFEST):
        path = Path(output)/name
        temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(document)
            os.replace(temporary, path)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise BuildError('installer_identity',
                             '发布身份记录写入失败，请检查输出目录权限。') from None
    return IDENTITY, digest


def _plain(path):
    """An absolute path without a trailing separator, as the Inno Setup macros expect."""
    return str(Path(path).resolve()).rstrip('\\/')


def _linked(entry):
    """True for a symbolic link, junction or any other reparse point."""
    try:
        details = entry.stat(follow_symlinks=False)
    except OSError:
        raise BuildError('installer_payload_read', f'应用目录条目无法读取：{entry.name}') from None
    return entry.is_symlink() or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


def _entries(directory):
    try:
        return sorted(os.scandir(directory), key=lambda item: item.name)
    except OSError:
        raise BuildError('installer_payload_read', f'应用目录无法读取：{Path(directory).name}') from None


def _walk(payload):
    """Every regular file below the payload, refusing links before anything is followed."""
    found, pending = [], ['']
    while pending:
        relative = pending.pop()
        current = payload/relative if relative else payload
        for entry in _entries(current):
            name = f'{relative}/{entry.name}' if relative else entry.name
            if _linked(entry):
                raise BuildError('installer_payload_link', f'应用目录中存在链接或目录联接：{name}')
            if entry.is_dir(follow_symlinks=False):
                pending.append(name)
            elif entry.is_file(follow_symlinks=False):
                found.append(name)
            else:
                raise BuildError('installer_payload_file', f'应用目录中存在非常规文件：{name}')
    return found


def _root_layout(payload):
    """The root must be this release's exact file list plus the single resources directory."""
    files, directories = set(), set()
    for entry in _entries(payload):
        if _linked(entry):
            raise BuildError('installer_payload_link', f'应用目录中存在链接或目录联接：{entry.name}')
        if entry.is_dir(follow_symlinks=False):
            directories.add(entry.name)
        elif entry.is_file(follow_symlinks=False):
            files.add(entry.name)
        else:
            raise BuildError('installer_payload_file', f'应用目录中存在非常规文件：{entry.name}')
    if directories != ROOT_DIRECTORIES:
        unexpected = sorted(directories - ROOT_DIRECTORIES)
        missing = sorted(ROOT_DIRECTORIES - directories)
        raise BuildError('installer_payload_directory',
                         f'应用目录顶层目录只能是 resources（缺少 {missing}，多余 {unexpected}）。')
    if files != ROOT_FILES:
        missing, unexpected = sorted(ROOT_FILES - files), sorted(files - ROOT_FILES)
        raise BuildError('installer_payload_file',
                         f'根文件与本次发布清单不符：缺少 {missing}，多余 {unexpected}。')


def _resources(payload):
    """Only the known component directories may live under resources."""
    root = payload/'resources'
    names = set()
    for entry in _entries(root):
        if _linked(entry):
            raise BuildError('installer_payload_link', f'组件资源中存在链接或目录联接：{entry.name}')
        if not entry.is_dir(follow_symlinks=False):
            raise BuildError('installer_resource_unexpected', f'组件资源目录下不接受文件：{entry.name}')
        names.add(entry.name)
    if not names <= RESOURCE_NAMES:
        raise BuildError('installer_resource_unexpected',
                         f'组件资源目录不接受：{sorted(names - RESOURCE_NAMES)}')
    for name in REQUIRED_RESOURCES:
        if name not in names:
            raise BuildError('installer_resource_missing', f'应用目录缺少组件资源 resources/{name}。')
    return names


def _manifest_files(resource, entrypoint):
    """The exact file set of one component: its own manifest plus everything the manifest lists."""
    try:
        manifest = json.loads((resource/MANIFEST_NAME).read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise BuildError('installer_resource_mismatch', f'{resource.name} 缺少可读的 {MANIFEST_NAME}。') from None
    if manifest.get('format') != 1 or manifest.get('entrypoint') != entrypoint:
        raise BuildError('installer_resource_mismatch', f'{resource.name} 的资源包不是 {entrypoint}。')
    recorded = {row['path'] for row in manifest['files']}
    return recorded | {MANIFEST_NAME}


def check_payload(directory):
    """Prove the payload is exactly one assembled application directory, then return it resolved."""
    payload = Path(directory)
    if not payload.is_dir():
        raise BuildError('installer_payload_missing', f'应用目录不存在：{payload}')
    payload = payload.resolve()
    if payload == REPO or (payload/'.git').exists():
        raise BuildError('installer_payload_project', '不接受项目目录本身：请传入 assemble_application.py 的输出目录。')
    present = set(_walk(payload))
    # The exact root list already proves the assembled files are there; nothing else is accepted.
    _root_layout(payload)
    _resources(payload)
    if json.loads((payload/MANAGED).read_text(encoding='utf-8-sig')) != MANAGED_RECORD:
        raise BuildError('installer_payload_managed', '应用目录不是托管应用构建（managed-application.json 不符）。')
    verify_desktop_build(payload, payload/APP_EXE)
    # Registered files only: the manifest must account for every file the component ships.
    for name, entrypoint in MANIFEST_RESOURCES:
        resource = payload/'resources'/name
        if not resource.is_dir():
            continue
        try:
            validate_resources(resource, entrypoint)
        except (ValueError, OSError) as failure:
            raise BuildError('installer_resource_mismatch',
                             f'组件资源 resources/{name} 与自己的清单不符：{failure}') from None
        expected = {f'resources/{name}/{path}' for path in _manifest_files(resource, entrypoint)}
        actual = {path for path in present if path.startswith(f'resources/{name}/')}
        if actual != expected:
            missing, unexpected = sorted(expected - actual)[:5], sorted(actual - expected)[:5]
            raise BuildError('installer_resource_unexpected',
                             f'组件资源 resources/{name} 含有未登记文件（缺少 {missing}，多余 {unexpected}）。')
    if (payload/'resources'/'qq').is_dir():
        from companion_qq.native_resources import verify_runtime

        try:
            verify_runtime(payload/'resources'/'qq')
        except Exception as failure:
            raise BuildError('installer_resource_mismatch',
                             f'组件资源 resources/qq 与固定版本不符：{failure}') from None
    files = [payload/path for path in present]
    summary = {'files': len(files), 'bytes': sum(path.stat().st_size for path in files),
               'exe_sha256': _digest(payload/APP_EXE),
               'build_record_sha256': _digest(payload/MANIFEST),
               'present_resources': sorted(name for name in RESOURCE_NAMES
                                           if (payload/'resources'/name).is_dir())}
    return payload, summary, payload_identity(payload, present)


def _source_record(paths):
    """Provenance of what produced the setup: this tool, the script and the shipped notes."""
    record = {}
    for label, path in paths:
        path = Path(path).resolve()
        record[label] = {'file': path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.name,
                         'sha256': _digest(path)}
    return record


def _compiler_version(iscc):
    try:
        finished = subprocess.run([str(iscc), '--version'], capture_output=True, text=True,
                                  timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    first = (finished.stdout or '').strip().splitlines()
    return first[0] if finished.returncode == 0 and first else None


def compile_setup(iscc, script, output, payload, version, record_sha256, identity_path,
                  identity_sha256):
    """Run the official compiler with the validated payload; the output path stays separate."""
    iscc = Path(iscc)
    if not iscc.is_file():
        raise BuildError('installer_iscc_missing', f'找不到 Inno Setup 编译器：{iscc}')
    output.mkdir(parents=True, exist_ok=True)
    command = [str(iscc),
               f'--define=PayloadDir={_plain(payload)}',
               f'--define=OutputDir={_plain(output)}',
               f'--define=PayloadIdentity={_plain(identity_path)}',
               f'--define=Version={version}',
               f'--define=ExpectedBuildRecordSha256={record_sha256}',
               f'--define=ExpectedPayloadIdentitySha256={identity_sha256}',
               '--no-ide-signtools', '--quiet-progress', str(script)]
    print(' '.join(command), flush=True)
    try:
        finished = subprocess.run(command, check=False)
    except OSError as failure:
        raise BuildError('installer_iscc_failed', f'无法运行 Inno Setup 编译器：{failure}') from None
    setup = output/f'Qiban-Setup-{version}.exe'
    if finished.returncode != 0 or not setup.is_file():
        raise BuildError('installer_iscc_failed',
                         'Inno Setup 编译失败；上面编译器的报错说明原因，本次不产出安装包。')
    return setup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True, help='assemble_application.py 的输出目录')
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--iscc', type=Path, default=DEFAULT_ISCC)
    parser.add_argument('--script', type=Path, default=SCRIPT)
    parser.add_argument('--version', default=DEFAULT_VERSION)
    parser.add_argument('--check-only', action='store_true',
                        help='只校验输入并输出发布身份摘要，不编译、不写任何文件')
    args = parser.parse_args(argv)
    try:
        if not VERSION_PATTERN.fullmatch(args.version):
            raise BuildError('installer_version', f'版本必须是 1 到 4 段数字，当前：{args.version}')
        payload, summary, identity = check_payload(args.app)
        output = Path(args.output_dir).resolve()
        if output == payload or output.is_relative_to(payload):
            raise BuildError('installer_output', '输出目录不能位于应用目录内。')
        script = Path(args.script).resolve()
        if not GLUE.is_file():
            raise BuildError('installer_glue_missing',
                             f'缺少安装胶水脚本：{GLUE.name}；没有它就不能准备配套升级。')
        document = identity_document(identity)
        identity_sha256 = hashlib.sha256(document).hexdigest()
        report = {'payload': str(payload), 'version': args.version, 'output': str(output),
                  'payload_files': summary['files'], 'payload_bytes': summary['bytes'],
                  'exe_sha256': summary['exe_sha256'],
                  'build_record_sha256': summary['build_record_sha256'],
                  'present_resources': summary['present_resources'],
                  'identity_file': IDENTITY, 'identity_sha256': identity_sha256}
        if args.check_only:
            # Deterministic and side-effect free: the identity is reported, never written.
            print(json.dumps({**report, 'compiled': False}))
            return 0
        identity_file, identity_sha256 = write_identity(output, document)
        setup = compile_setup(args.iscc, script, output, payload, args.version,
                              summary['build_record_sha256'], output/identity_file, identity_sha256)
        setup_bytes, setup_sha256 = setup.stat().st_size, _digest(setup)
        record = {'format': FORMAT, 'version': args.version,
                  'setup': {'file': setup.name, 'bytes': setup_bytes, 'sha256': setup_sha256},
                  'payload': {'directory': payload.relative_to(REPO).as_posix()
                              if payload.is_relative_to(REPO) else payload.name,
                              'exe': APP_EXE, **summary},
                  'identity': {'file': identity_file, 'sha256': identity_sha256,
                               'target_manifest': {'file': TARGET_MANIFEST,
                                                   'sha256': identity_sha256}},
                  'sources': _source_record((('builder', Path(__file__)), ('script', script),
                                             ('notes', NOTES), ('glue', GLUE))),
                  'compiler': {'name': 'inno-setup', 'iscc': _compiler_version(args.iscc)},
                  'scope': {'first_install': True, 'same_build_repair': True,
                            'replace_never_run_program': True,
                            'paired_upgrade': 'installed build prepares via its own frozen worker',
                            'paired_upgrade_requires_old_maintenance_verb': True,
                            'recovery_shortcut_recorded': True,
                            'forward_old_only_cleanup': True,
                            # The installer itself never opens, copies or moves personal data. A
                            # paired upgrade does change which generation is selected, but only
                            # through the installed build's own maintenance worker (preservation,
                            # clone, pointer switch) - never through this tool.
                            'personal_data_touched_by_installer': False,
                            'paired_upgrade_changes_data_through_old_worker': True},
                  'signed': False}
        (output/RECORD).write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                                  encoding='utf8')
        print(json.dumps({**report, 'setup': str(setup), 'setup_bytes': setup_bytes,
                          'setup_sha256': setup_sha256, 'record': str(output/RECORD),
                          'compiled': True}))
    except BuildError as failure:
        print(f'installer error {failure.code}: {failure.message}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
