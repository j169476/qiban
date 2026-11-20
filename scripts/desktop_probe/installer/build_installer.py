"""Build a per-user installer from a verified application directory.

The payload manifest records every installed path and digest. The compiler uses the
same manifest to share identical file data. Setup also embeds the manifest under a
private name for the installed maintenance worker during repairs and upgrades.
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
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE.parent
REPO = PROBE.parent.parent
SCRIPT = HERE/'Qiban.iss'
NOTES = HERE/'INSTALL-NOTES.txt'
GLUE = HERE/'prepare_upgrade.ps1'
DEFAULT_OUTPUT = REPO/'.dev/refactor-r0/desktop/installer-174/output'
DEFAULT_ISCC = REPO/'.dev/refactor-r0/desktop/installer-174/tools/inno/ISCC.exe'
DEFAULT_VERSION = '0.9.9.4'
RECORD = 'installer-build.json'
IDENTITY = 'qiban-payload.json'
# Byte-identical copy of IDENTITY under a setup-only name. The setup embeds this one (dontcopy) so
# the maintenance glue can be handed ``-TargetManifestPath`` without guessing which of two identical
# [Files] entries to extract. It is never installed and never compared directly by the gate.
TARGET_MANIFEST = 'qiban-target-payload.json'
IDENTITY_FORMAT = 1
FORMAT = 1
APP_EXE = 'Qiban.exe'
# The published uninstall entry. assemble_application.py writes it as one byte-identical copy of
# APP_EXE, it is checked again below, and it is installed with the rest of the payload unchanged.
ALIAS_EXE = 'uninstall.exe'
MANAGED = 'managed-application.json'
MANIFEST_NAME = 'runtime-manifest.json'
MANAGED_RECORD = {'mode': 'managed', 'format': 1}
# Exactly the files one assembled application directory publishes at its root.
ROOT_FILES = frozenset({
    'CsWinRT-LICENSE.txt', 'Microsoft.Web.WebView2.Core.xml', 'Microsoft.Web.WebView2.WinForms.xml',
    'Qiban-LICENSE.txt', 'Qiban.exe', 'README.txt', 'WebView2-SDK.txt', 'Windows-SDK.rtf',
    'desktop-build.json', 'desktop-sources.zip', 'dotnet-desktop-LICENSE.txt',
    'dotnet-runtime-LICENSE.txt', 'dotnet-runtime-THIRD-PARTY-NOTICES.txt',
    'managed-application.json', 'uninstall.exe'})
ROOT_DIRECTORIES = frozenset({'resources'})
RESOURCE_NAMES = frozenset({'storage', 'postgres', 'speech', 'wechat', 'qq'})
REQUIRED_RESOURCES = ('storage', 'postgres')
MANIFEST_RESOURCES = (('storage', 'Qiban.Storage.exe'), ('postgres', 'bin/postgres.exe'),
                      ('speech', 'Qiban.Speech.exe'), ('wechat', 'Qiban.WeChatReader.exe'))
VERSION_PATTERN = re.compile(r'\d+(\.\d+){1,3}')
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
sys.path.insert(0, str(PROBE))
sys.path.insert(0, str(REPO/'src'))
from assemble_application import resource_manifest  # noqa: E402
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
    found, directories, pending = set(), set(), ['']
    while pending:
        relative = pending.pop()
        current = payload/relative if relative else payload
        for entry in _entries(current):
            name = f'{relative}/{entry.name}' if relative else entry.name
            if _linked(entry):
                raise BuildError('installer_payload_link', f'应用目录中存在链接或目录联接：{name}')
            if entry.is_dir(follow_symlinks=False):
                directories.add(name)
                pending.append(name)
            elif entry.is_file(follow_symlinks=False):
                found.add(name)
            else:
                raise BuildError('installer_payload_file', f'应用目录中存在非常规文件：{name}')
    return found, directories


def _root_layout(paths, folders):
    """The root must be this release's exact file list plus the single resources directory."""
    files = {name for name in paths if '/' not in name}
    directories = {name for name in folders if '/' not in name}
    if directories != ROOT_DIRECTORIES:
        unexpected = sorted(directories - ROOT_DIRECTORIES)
        missing = sorted(ROOT_DIRECTORIES - directories)
        raise BuildError('installer_payload_directory',
                         f'应用目录顶层目录只能是 resources（缺少 {missing}，多余 {unexpected}）。')
    if files != ROOT_FILES:
        missing, unexpected = sorted(ROOT_FILES - files), sorted(files - ROOT_FILES)
        raise BuildError('installer_payload_file',
                         f'根文件与本次发布清单不符：缺少 {missing}，多余 {unexpected}。')


def _resources(paths, folders):
    """Only the known component directories may live under resources."""
    if any(name.startswith('resources/') and name.count('/') == 1 for name in paths):
        raise BuildError('installer_resource_unexpected', '组件资源目录下不接受文件。')
    names = {name.split('/')[1] for name in folders if name.startswith('resources/') and name.count('/') == 1}
    if not names <= RESOURCE_NAMES:
        raise BuildError('installer_resource_unexpected',
                         f'组件资源目录不接受：{sorted(names - RESOURCE_NAMES)}')
    for name in REQUIRED_RESOURCES:
        if name not in names:
            raise BuildError('installer_resource_missing', f'应用目录缺少组件资源 resources/{name}。')
    return names


def check_payload(directory):
    """Prove the payload is exactly one assembled application directory, then return it resolved."""
    payload = Path(directory)
    if not payload.is_dir():
        raise BuildError('installer_payload_missing', f'应用目录不存在：{payload}')
    payload = payload.resolve()
    if payload == REPO or (payload/'.git').exists():
        raise BuildError('installer_payload_project', '不接受项目目录本身：请传入 assemble_application.py 的输出目录。')
    present, folders = _walk(payload)
    _root_layout(present, folders)
    resources = _resources(present, folders)
    if json.loads((payload/MANAGED).read_text(encoding='utf-8-sig')) != MANAGED_RECORD:
        raise BuildError('installer_payload_managed', '应用目录不是托管应用构建（managed-application.json 不符）。')
    verify_desktop_build(payload, payload/APP_EXE)
    # The uninstall entry is the same program under its own name, never a second build: a copy that
    # differs by one byte is refused here, so the installed alias cannot be something else.
    identity = payload_identity(payload, present)
    hashes = {row['path']: row['sha256'] for row in identity['files']}
    if hashes[ALIAS_EXE] != hashes[APP_EXE]:
        raise BuildError('installer_payload_alias',
                         '卸载入口 uninstall.exe 与主程序 Qiban.exe 不是同一份程序文件。')
    # Registered files only: the manifest must account for every file the component ships.
    for name, entrypoint in MANIFEST_RESOURCES:
        resource = payload/'resources'/name
        if name not in resources:
            continue
        try:
            rows = resource_manifest(resource, entrypoint)
            for row in rows:
                if hashes.get(f"resources/{name}/{row['path']}") != row['sha256']:
                    raise ValueError('Resource mismatch')
        except (ValueError, OSError, KeyError, TypeError) as failure:
            raise BuildError('installer_resource_mismatch',
                             f'组件资源 resources/{name} 与自己的清单不符：{failure}') from None
        expected = {f"resources/{name}/{row['path']}" for row in rows} | {f'resources/{name}/{MANIFEST_NAME}'}
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
    summary = {'files': identity['count'], 'bytes': identity['bytes'], 'exe_sha256': hashes[APP_EXE],
               'alias_sha256': hashes[ALIAS_EXE], 'build_record_sha256': hashes[MANIFEST],
               'present_resources': sorted(resources)}
    return payload, summary, identity


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


def payload_entries(payload, identity):
    # ISCC stores a repeated Source once, including when the installed filenames differ.
    sources = {}
    lines = ['[Dirs]']
    for directory, children, files in os.walk(payload):
        if not children and not files:
            name = Path(directory).relative_to(payload).as_posix()
            lines.append(f'Name: "{{app}}\\{installer_name(name)}"')
    lines.append('[Files]')
    for row in identity['files']:
        name = Path(row['path'])
        source = sources.setdefault((row['bytes'], row['sha256']), row['path'])
        folder = '{app}'
        if name.parent != Path('.'):
            folder += '\\' + installer_name(name.parent.as_posix())
        lines.append(f'Source: "{{#PayloadDir}}\\{installer_name(source)}"; DestDir: "{folder}"; '
                     f'DestName: "{installer_name(name.name)}"; Flags: ignoreversion')
    return '\n'.join(lines) + '\n'


def installer_name(name):
    if any(c in name for c in '"{}\r\n') or Path(name).is_absolute() or '..' in Path(name).parts:
        raise BuildError('installer_payload_path', f'无法打包此路径：{name}')
    return name.replace('/', '\\')


def compile_setup(iscc, script, output, payload, version, record_sha256, identity_path,
                  identity_sha256, *, file_table=None):
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
    if file_table:
        command.insert(1, f'--define=PayloadEntries={_plain(file_table)}')
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
                  'exe_sha256': summary['exe_sha256'], 'alias_sha256': summary['alias_sha256'],
                  'build_record_sha256': summary['build_record_sha256'],
                  'present_resources': summary['present_resources'],
                  'identity_file': IDENTITY, 'identity_sha256': identity_sha256}
        if args.check_only:
            # Deterministic and side-effect free: the identity is reported, never written.
            print(json.dumps({**report, 'compiled': False}))
            return 0
        identity_file, identity_sha256 = write_identity(output, document)
        with tempfile.TemporaryDirectory(prefix='qiban-setup-') as temporary:
            entries = Path(temporary) / 'payload.iss'
            entries.write_text(payload_entries(payload, identity), encoding='utf-8-sig')
            setup = compile_setup(args.iscc, script, output, payload, args.version,
                                  summary['build_record_sha256'], output/identity_file, identity_sha256,
                                  file_table=entries)
        setup_bytes, setup_sha256 = setup.stat().st_size, _digest(setup)
        record = {'format': FORMAT, 'version': args.version,
                  'setup': {'file': setup.name, 'bytes': setup_bytes, 'sha256': setup_sha256},
                  'payload': {'directory': payload.relative_to(REPO).as_posix()
                              if payload.is_relative_to(REPO) else payload.name,
                              'exe': APP_EXE, 'alias': ALIAS_EXE, **summary},
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
                            'paired_upgrade_changes_data_through_old_worker': True,
                            # UNINSTALL: the published alias is the same program file, the Start
                            # Menu entry and the Windows entry both open the application's own
                            # preparation page, and only its one fixed internal parameter continues
                            # into this uninstaller. A silent uninstall keeps its previous rules.
                            'uninstall_alias_sha256_matches_exe': True,
                            'uninstall_preparation_page': True,
                            'uninstall_silent_unchanged': True},
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
