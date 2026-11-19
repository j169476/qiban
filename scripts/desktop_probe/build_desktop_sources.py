"""Record one desktop shell's own sources beside the executable that was published from them.

The shell is published from ``scripts/desktop_probe`` only. The storage and PostgreSQL resource
packages are separate builds with their own ``upstream-source`` archives (for example the whole
repository recorded when the storage package was built), so a reused resource package can never
describe a later desktop shell. This tool writes two files into the published directory during
the same ``build.ps1`` pipeline that produced the executable:

    desktop-sources.zip    the local desktop sources this shell was published from
    desktop-build.json     format 1: executable file name and digest, the archive and its digest,
                           every archived path with its digest, and the two compile switches

``pre`` records the whitelist digest before ``dotnet publish``; ``post`` reads the same whitelist
again afterwards and fails the build when any input changed while publishing. The recorded archive
therefore always belongs to the shell recorded next to it: it is never assembled later from an
arbitrary source directory. Only the whitelist is read — top-level ``*.cs``, ``*.csproj``,
``packages.lock.json``, ``build.ps1`` and this recorder itself in the probe directory, its
``Assets`` and ``licenses`` trees, the public-page generator and its five published documents. Nothing under ``.dev``, no installation configuration,
``runtime.json``, log, account, cache or test data is opened, and refused paths fail the build.

Nothing here signs the executable, adds an update source or claims that installation, upgrade or a
paired program/data rollback exists. Non-zero exit means the build failed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

FORMAT = 1
ARCHIVE = 'desktop-sources.zip'
MANIFEST = 'desktop-build.json'
PUBLISHED_EXE = 'Qiban.DesktopProbe.exe'
SNAPSHOT_FIELDS = frozenset({'format', 'files'})
ROW_FIELDS = frozenset({'path', 'sha256'})
MANIFEST_FIELDS = frozenset({'format', 'exe', 'source_archive', 'files', 'compiled', 'signed'})
EXE_FIELDS = frozenset({'file', 'sha256'})
ARCHIVE_FIELDS = frozenset({'file', 'sha256'})
COMPILED_FIELDS = frozenset({'managed_application', 'subscription_validation'})
TOP_NAMES = frozenset({'build.ps1', 'build_desktop_sources.py', 'build_public_pages.py', 'packages.lock.json'})
TOP_SUFFIXES = frozenset({'.cs', '.csproj'})
TREES = ('Assets', 'licenses')
PROJECT_FILE = 'LICENSE'
EMBEDDED = ('THIRD_PARTY_NOTICES.md', 'docs/桌面声明.md', 'docs/微信支持范围.md', 'docs/模型选择参考.md')
FORBIDDEN_PARTS = frozenset({
    '.dev', 'accounts', 'active-generation.json', 'cache', 'installation-status.json',
    'installation.json', 'logs', 'runtime.json', 'storage-config.json', 'temp', 'test-data'})
FORBIDDEN_SUFFIXES = ('.db', '.dll', '.exe', '.key', '.log', '.pdb', '.pem', '.sqlite', '.tmp', '.zip')
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
UNSAFE = '桌面源码目录中存在指向其他位置的链接，请人工检查。'


class BuildError(Exception):
    """A build-record refusal; the CLI prints the stable code and the reason."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _reparse(path):
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


def _root(project):
    try:
        resolved = Path(project).resolve(strict=True)
    except (OSError, RuntimeError):
        raise BuildError('desktop_sources_root', '项目目录不可用。') from None
    if not resolved.is_dir():
        raise BuildError('desktop_sources_root', '项目目录不可用。')
    return resolved


def _directory(path, code='desktop_sources_missing'):
    path = Path(path)
    if _reparse(path):
        raise BuildError('desktop_sources_path', UNSAFE)
    if not path.is_dir():
        raise BuildError(code, f'源码目录缺失：{path}')
    return path


def _file(path, code='desktop_sources_read'):
    path = Path(path)
    if _reparse(path):
        raise BuildError('desktop_sources_path', UNSAFE)
    if not path.is_file():
        raise BuildError(code, f'源码文件缺失：{path}')
    return path


def _digest(path):
    number = hashlib.sha256()
    try:
        with Path(path).open('rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                number.update(block)
    except OSError:
        raise BuildError('desktop_sources_read', f'源码文件无法读取：{path}') from None
    return number.hexdigest()


def _forbidden(relative):
    if any(part in FORBIDDEN_PARTS for part in relative.split('/')):
        raise BuildError('desktop_sources_forbidden', f'源码白名单不接受该路径：{relative}')
    if relative.lower().endswith(FORBIDDEN_SUFFIXES):
        raise BuildError('desktop_sources_forbidden', f'源码白名单不接受该文件类型：{relative}')


def sources(project):
    """Every file this shell needs to be published, relative to the project and hashed."""
    project = _root(project)
    probe = _directory(project/'scripts'/'desktop_probe')
    picked = []
    for entry in sorted(probe.iterdir()):
        if _reparse(entry):
            raise BuildError('desktop_sources_path', UNSAFE)
        if entry.is_file() and (entry.name in TOP_NAMES or entry.suffix in TOP_SUFFIXES):
            picked.append(entry)
    for tree in TREES:
        base = _directory(probe/tree)
        for entry in sorted(base.rglob('*')):
            if _reparse(entry):
                raise BuildError('desktop_sources_path', UNSAFE)
            if entry.is_file():
                picked.append(entry)
    picked.append(_file(project/PROJECT_FILE))
    picked.extend(_file(project/name) for name in EMBEDDED)
    rows = []
    for path in sorted(picked, key=lambda item: item.relative_to(project).as_posix()):
        relative = path.relative_to(project).as_posix()
        _forbidden(relative)
        rows.append({'path': relative, 'sha256': _digest(path)})
    if not rows:
        raise BuildError('desktop_sources_missing', '桌面源码白名单为空。')
    return rows


def _rows(value, code):
    if not isinstance(value, list):
        raise BuildError(code, '文件摘要列表格式不符。')
    rows, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            raise BuildError(code, '文件摘要列表字段不符。')
        path, sha = row['path'], row['sha256']
        if not isinstance(path, str) or not path or path.startswith('/') or '..' in Path(path).parts:
            raise BuildError(code, '文件路径无效。')
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise BuildError(code, '文件摘要无效。')
        if path in seen:
            raise BuildError(code, '文件摘要列表存在重复路径。')
        seen.add(path)
        rows.append({'path': path, 'sha256': sha})
    if rows != sorted(rows, key=lambda row: row['path']):
        raise BuildError(code, '文件摘要列表未按路径排序。')
    return rows


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    if _reparse(path.parent):
        raise BuildError('desktop_sources_path', UNSAFE)
    try:
        with temporary.open('w', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise BuildError('desktop_sources_write', '构建记录写入失败，请检查磁盘与目录权限。') from None
    return path


def write_snapshot(project, path):
    """Record the whitelist before publishing so ``post`` can prove it did not change."""
    rows = sources(project)
    _write(path, {'format': FORMAT, 'files': rows})
    return rows


def _read_snapshot(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise BuildError('desktop_sources_snapshot', '发布前源码快照无法读取。') from None
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS:
        raise BuildError('desktop_sources_snapshot', '发布前源码快照字段不符。')
    if type(value['format']) is not int or value['format'] != FORMAT:
        raise BuildError('desktop_sources_snapshot', '发布前源码快照版本不符。')
    return _rows(value['files'], 'desktop_sources_snapshot')


def verify_snapshot(before, project):
    """Fail the build when any whitelisted input changed between the snapshot and now."""
    after = sources(project)
    if before == after:
        return after
    known = {row['path']: row['sha256'] for row in before}
    now = {row['path']: row['sha256'] for row in after}
    changed = sorted(path for path, sha in now.items() if path in known and known[path] != sha)
    added, removed = sorted(set(now) - set(known)), sorted(set(known) - set(now))
    detail = '；'.join(part for part in (
        f'变化 {", ".join(changed)}' if changed else '',
        f'新增 {", ".join(added)}' if added else '',
        f'删除 {", ".join(removed)}' if removed else '') if part)
    raise BuildError('desktop_sources_changed', f'桌面外壳源码在发布期间发生变化，构建失败：{detail}')


def write_archive(project, path, rows):
    """One deterministic archive of exactly the snapshot rows; no file outside them is read."""
    project, path = _root(project), Path(path)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    if _reparse(path.parent):
        raise BuildError('desktop_sources_path', UNSAFE)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for row in rows:
                info = zipfile.ZipInfo(row['path'], date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                bundle.writestr(info, (project/row['path']).read_bytes())
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise BuildError('desktop_sources_archive', '源码归档写入失败，请检查磁盘与目录权限。') from None
    return path


def archived(path):
    """Path and digest of every member actually inside the written archive."""
    try:
        with zipfile.ZipFile(Path(path)) as bundle:
            names = bundle.namelist()
            if len(set(names)) != len(names):
                raise BuildError('desktop_sources_archive', '源码归档存在重复条目。')
            return {name: hashlib.sha256(bundle.read(name)).hexdigest() for name in names}
    except (OSError, zipfile.BadZipFile):
        raise BuildError('desktop_sources_archive', '源码归档无法读取。') from None


def record(project, app_dir, exe, archive, manifest, snapshot, managed, subscription):
    """Verify the snapshot, then write the archive and the format 1 record of this shell."""
    if subscription and not managed:
        raise BuildError('desktop_build_flags', 'SubscriptionValidation 需要 ManagedApplication。')
    project = _root(project)
    rows = verify_snapshot(_read_snapshot(snapshot), project)
    app_dir = _directory(app_dir, 'desktop_build_output')
    if not isinstance(exe, str) or not exe or Path(exe).name != exe:
        raise BuildError('desktop_build_exe', '必须传入发布目录内的可执行文件名。')
    shell = _file(app_dir/exe, 'desktop_build_exe')
    archive = write_archive(project, archive, rows)
    names = archived(archive)
    recorded = {row['path']: row['sha256'] for row in rows}
    if set(names) != set(recorded):
        raise BuildError('desktop_sources_archive', '源码归档与白名单不一致。')
    if any(names[path] != sha for path, sha in recorded.items()):
        raise BuildError('desktop_sources_changed', '桌面外壳源码在归档期间发生变化，构建失败。')
    value = {'format': FORMAT,
             'exe': {'file': exe, 'sha256': _digest(shell)},
             'source_archive': {'file': Path(archive).name, 'sha256': _digest(archive)},
             'files': [{'path': path, 'sha256': names[path]} for path in sorted(names)],
             'compiled': {'managed_application': bool(managed),
                          'subscription_validation': bool(subscription)},
             'signed': False}
    _write(manifest, value)
    return value


def _read_manifest(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf8'))
    except FileNotFoundError:
        raise BuildError('desktop_build_record_missing', f'发布目录缺少 {MANIFEST}，无法核对程序与源码。') from None
    except (OSError, ValueError):
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 无法读取。') from None
    if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 字段不符。')
    if type(value['format']) is not int or value['format'] != FORMAT:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 版本不符。')
    if value['signed'] is not False:
        raise BuildError('desktop_build_signature_claim', '构建记录不得声称程序已签名。')
    exe = value['exe']
    if not isinstance(exe, dict) or set(exe) != EXE_FIELDS:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 可执行文件记录字段不符。')
    if not isinstance(exe['file'], str) or not exe['file'] or Path(exe['file']).name != exe['file']:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 可执行文件名无效。')
    source = value['source_archive']
    if not isinstance(source, dict) or set(source) != ARCHIVE_FIELDS:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 源码归档记录字段不符。')
    if source['file'] != ARCHIVE:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 源码归档文件名不符。')
    if not isinstance(source['sha256'], str) or len(source['sha256']) != 64:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 源码归档摘要无效。')
    compiled = value['compiled']
    if not isinstance(compiled, dict) or set(compiled) != COMPILED_FIELDS:
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 编译开关记录字段不符。')
    if any(not isinstance(compiled[name], bool) for name in COMPILED_FIELDS):
        raise BuildError('desktop_build_record_invalid', f'{MANIFEST} 编译开关记录不是布尔值。')
    value['files'] = _rows(value['files'], 'desktop_build_record_invalid')
    return value


def verify(app_dir, exe_path):
    """Refuse a published directory whose program and recorded sources do not belong together."""
    app_dir = Path(app_dir)
    value = _read_manifest(app_dir/MANIFEST)
    archive_path = app_dir/ARCHIVE
    names = archived(archive_path)
    recorded = {row['path']: row['sha256'] for row in value['files']}
    if set(names) != set(recorded):
        raise BuildError('desktop_sources_archive', '源码归档内容与构建清单记录的路径不一致。')
    if any(names[path] != sha for path, sha in recorded.items()):
        raise BuildError('desktop_sources_archive', '源码归档内容与构建清单记录的摘要不一致。')
    if _digest(archive_path) != value['source_archive']['sha256']:
        raise BuildError('desktop_build_archive_mismatch', '源码归档摘要与构建清单不符。')
    shell = Path(exe_path)
    if _reparse(shell):
        raise BuildError('desktop_build_exe_missing', '发布目录中的可执行文件不可用。')
    if not shell.is_file():
        raise BuildError('desktop_build_exe_missing', '发布目录中缺少本次构建的可执行文件。')
    if _digest(shell) != value['exe']['sha256']:
        raise BuildError('desktop_build_exe_mismatch', '可执行文件与本次构建清单记录的摘要不符。')
    if value['exe']['file'] not in (shell.name, PUBLISHED_EXE):
        raise BuildError('desktop_build_exe_name', '构建记录的可执行文件名与目录内程序不符。')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    steps = parser.add_subparsers(dest='step', required=True)
    before = steps.add_parser('pre', help='发布前记录白名单源码摘要')
    before.add_argument('--project', type=Path, required=True)
    before.add_argument('--snapshot', type=Path, required=True)
    after = steps.add_parser('post', help='发布后核对源码未变并写入归档与清单')
    after.add_argument('--project', type=Path, required=True)
    after.add_argument('--snapshot', type=Path, required=True)
    after.add_argument('--app-dir', type=Path, required=True)
    after.add_argument('--exe', default=PUBLISHED_EXE)
    after.add_argument('--archive', type=Path)
    after.add_argument('--manifest', type=Path)
    after.add_argument('--managed', action='store_true')
    after.add_argument('--subscription', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.step == 'pre':
            rows = write_snapshot(args.project, args.snapshot)
            print(json.dumps({'snapshot': str(args.snapshot), 'files': len(rows)}))
        else:
            value = record(args.project, args.app_dir, args.exe,
                           args.archive or args.app_dir/ARCHIVE,
                           args.manifest or args.app_dir/MANIFEST,
                           args.snapshot, args.managed, args.subscription)
            print(json.dumps({'exe': value['exe']['file'], 'sha256': value['exe']['sha256'],
                              'source_archive': value['source_archive']['file'],
                              'files': len(value['files'])}))
    except BuildError as failure:
        print(f'build-record error {failure.code}: {failure.message}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
