"""Published program identities and the maintenance carrier's file trees.

The installer already ships one deterministic record per build (``qiban-payload.json``): every
published file with its byte count and SHA-256, itself excluded, and the digest of that
document is the program identity the installer gate compares. An upgrade transaction reuses
exactly that rule instead of inventing a second one: the carrier is copied file by file with
the digest computed while writing, and every later use re-proves the whole tree against the
document that travels with it.

File-level correctness on the way back matters as much as on the way out. Rolling a program
back means writing the previous published set *and* removing the files only the newer build
published - decided strictly by comparing the two verified documents, never by looking at
timestamps, directory contents or anything a person could have put there. A path that is in
neither document is never touched, and a rollback is only reported after the whole published
set re-hashes to the previous identity and no path of the newer document is left behind.

Windows refuses a path longer than MAX_PATH to its file APIs unless the path is given in the
``\\\\?\\`` form, and an installation may run with ``LongPathsEnabled`` turned off - which a normal
desktop installation does. Every file operation below therefore goes through ``native_io_path``:
the same absolute path, only in the form those APIs accept. Records, comparisons, the carrier's
own directory and everything a caller sees stay ordinary absolute paths - the extended form
exists only for the length of one call and is never written down.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
from pathlib import Path

from install_windows import protect_directory

from companion_core.errors import AssistantError

from . import journal

IDENTITY = 'qiban-payload.json'
CARRIER = 'carrier.json'
IDENTITY_FORMAT = 1
CARRIER_FORMAT = 1
MAX_IDENTITY = 16*1024*1024
MAX_PATH = 400
DIGEST = journal.DIGEST
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
IDENTITY_FIELDS = frozenset({'format', 'count', 'bytes', 'files'})
ROW_FIELDS = frozenset({'path', 'bytes', 'sha256'})
CARRIER_FIELDS = frozenset({'format', 'installation', 'identity_sha256', 'count', 'bytes',
                            'transaction', 'created_at', 'roles'})


def _refuse(code, message):
    raise AssistantError(code, message)


def _reparse(path):
    try:
        details = os.lstat(native_io_path(path))
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


def native_io_path(path):
    """The form one Win32 file operation needs for a path that may pass MAX_PATH.

    A customer installation can run with ``LongPathsEnabled`` turned off, where the wide file APIs
    still refuse anything past MAX_PATH - including the carrier's temporary directory, which is a
    few characters longer than the carrier itself. Windows accepts such a path when it is fully
    qualified and prefixed with ``\\\\?\\`` (or ``\\\\?\\UNC\\`` for a share), so each call site of
    this module performs its operation through that form.

    The value is derived per call from the ordinary path: an already prefixed path is returned
    unchanged, a relative one is made absolute first, and the result is only ever handed to a file
    operation - it is never stored in a record, compared with another path or shown to a person.
    Other platforms need nothing of this and get the path back unchanged.
    """
    if os.name != 'nt':
        return os.fspath(path)
    text = os.fspath(path)
    if text.startswith('\\\\?\\') or text.startswith('\\\\.\\'):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith('\\\\'):
        return '\\\\?\\UNC\\'+absolute[2:]
    return '\\\\?\\'+absolute


def digest(path):
    number = hashlib.sha256()
    try:
        with open(native_io_path(path), 'rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                number.update(block)
    except OSError:
        _refuse('upgrade_program_read', '程序文件无法读取。')
    return number.hexdigest()


def parts(name):
    """A published relative path, split for a safe join below a program directory."""
    if (not isinstance(name, str) or not 0 < len(name) <= MAX_PATH or name.startswith('/')
            or '\\' in name or ':' in name):
        _refuse('upgrade_program_invalid', '发布身份记录中的文件路径无效。')
    pieces = name.split('/')
    if any(piece in ('', '.', '..') for piece in pieces):
        _refuse('upgrade_program_invalid', '发布身份记录中的文件路径无效。')
    return pieces


def document(app):
    """The published identity of one program directory, validated strictly."""
    file = Path(app)/IDENTITY
    try:
        with open(native_io_path(file), 'rb') as stream:
            raw = stream.read(MAX_IDENTITY+1)
    except OSError:
        _refuse('upgrade_program_missing', '程序目录缺少发布身份记录，无法把它作为升级对象。')
    if not 0 < len(raw) <= MAX_IDENTITY:
        _refuse('upgrade_program_invalid', '程序目录的发布身份记录无效。')
    try:
        value = json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError):
        _refuse('upgrade_program_invalid', '程序目录的发布身份记录无效。')
    if not isinstance(value, dict) or set(value) != IDENTITY_FIELDS:
        _refuse('upgrade_program_invalid', '程序目录的发布身份记录字段不符。')
    if type(value['format']) is not int or value['format'] != IDENTITY_FORMAT:
        _refuse('upgrade_program_invalid', '程序目录的发布身份记录版本不符。')
    if type(value['count']) is not int or type(value['bytes']) is not int or not value['files']:
        _refuse('upgrade_program_invalid', '程序目录的发布身份记录无效。')
    rows, seen, total = [], set(), 0
    for row in value['files']:
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            _refuse('upgrade_program_invalid', '发布身份记录中的文件条目无效。')
        parts(row['path'])
        if (type(row['bytes']) is not int or row['bytes'] < 0 or not isinstance(row['sha256'], str)
                or not DIGEST.fullmatch(row['sha256']) or row['path'] in seen):
            _refuse('upgrade_program_invalid', '发布身份记录中的文件条目无效。')
        seen.add(row['path'])
        total += row['bytes']
        rows.append({'path': row['path'], 'bytes': row['bytes'], 'sha256': row['sha256']})
    if value['count'] != len(rows) or value['bytes'] != total:
        _refuse('upgrade_program_invalid', '发布身份记录的计数与文件条目不相等。')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'count': len(rows), 'bytes': total,
            'files': rows}


def paths(published):
    return {row['path']: row for row in published['files']}


def tree(root, identity_sha256):
    """Where one program copy lives: `<root>/upgrade/programs/<64-hex identity>`."""
    if not isinstance(identity_sha256, str) or not DIGEST.fullmatch(identity_sha256):
        _refuse('upgrade_program_invalid', '程序副本标识无效。')
    return journal.directory(root)/journal.PROGRAMS/identity_sha256


def copy_carrier(root, app, published, *, installation, transaction, roles):
    """Copy one published program beside the data root and prove every byte of the copy.

    The copy is the transaction's execution carrier: it must be complete and verified *before*
    the pointer may move or the program directory may be overwritten, because from then on it
    is the only actor that can still read this machine's records and switch back. Its identity
    document travels with it, so a later use re-proves the tree instead of trusting the path.
    """
    target = tree(root, published['sha256'])
    if os.path.exists(native_io_path(target)):
        verify(target, published)
        _write_carrier_record(target, published, installation=installation,
                              transaction=transaction, roles=roles)
        return target
    parent = journal.directory(root, create=True)/journal.PROGRAMS
    try:
        os.makedirs(native_io_path(parent), exist_ok=True)
        protect_directory(parent)
    except Exception:
        _refuse('upgrade_directory_permissions', '无法设置程序副本目录权限，未继续维护操作。')
    temporary = parent/(published['sha256']+'.tmp-'+secrets.token_hex(4))
    try:
        os.mkdir(native_io_path(temporary))
        for row in published['files']:
            _copy(Path(app).joinpath(*parts(row['path'])),
                  temporary.joinpath(*parts(row['path'])), row)
        shutil.copyfile(native_io_path(Path(app)/IDENTITY), native_io_path(temporary/IDENTITY))
        if digest(temporary/IDENTITY) != published['sha256']:
            _refuse('upgrade_program_changed', '程序发布身份记录在复制期间变化，未继续维护操作。')
        os.rename(native_io_path(temporary), native_io_path(target))
    except BaseException:
        # The temporary tree is a few characters longer than the carrier it becomes, which is
        # exactly the path Windows refuses without the extended form: this removal has to work.
        shutil.rmtree(native_io_path(temporary), ignore_errors=True)
        raise
    try:
        protect_directory(target)
    except Exception:
        _refuse('upgrade_directory_permissions',
                '程序副本已复制并校验，但无法设置目录权限，请人工检查。')
    verify(target, published)
    _write_carrier_record(target, published, installation=installation,
                          transaction=transaction, roles=roles)
    return target


def _write_carrier_record(directory, published, *, installation, transaction, roles):
    record = {'format': CARRIER_FORMAT, 'installation': installation,
              'identity_sha256': published['sha256'], 'count': published['count'],
              'bytes': published['bytes'], 'transaction': transaction,
              'created_at': journal.utcnow(), 'roles': sorted(set(roles))}
    if not isinstance(record['roles'], list) or not record['roles']:
        _refuse('upgrade_program_invalid', '程序副本角色无效。')
    target = Path(directory)/CARRIER
    temporary = target.with_name(target.name+'.tmp')
    try:
        with open(native_io_path(temporary), 'w', encoding='utf8') as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(native_io_path(temporary), native_io_path(target))
    except OSError:
        _refuse('upgrade_write', '程序副本标记写入失败，请检查磁盘与目录权限。')
    return record


def carrier_record(directory):
    """The ownership mark of one program copy; a tree without it is never treated as ours."""
    file = Path(directory)/CARRIER
    try:
        with open(native_io_path(file), encoding='utf8') as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        _refuse('upgrade_program_invalid', '程序副本缺少归属标记。')
    if (not isinstance(value, dict) or set(value) != CARRIER_FIELDS
            or type(value['format']) is not int or value['format'] != CARRIER_FORMAT
            or not isinstance(value['identity_sha256'], str)
            or not DIGEST.fullmatch(value['identity_sha256'])
            or type(value['count']) is not int or type(value['bytes']) is not int
            or not isinstance(value['installation'], str)
            or not journal.IDENTIFIER.fullmatch(value['installation'])
            or not isinstance(value['roles'], list) or not value['roles']):
        _refuse('upgrade_program_invalid', '程序副本归属标记无效。')
    return value


def _copy(source, destination, row):
    if _reparse(source) or not os.path.isfile(native_io_path(source)):
        _refuse('upgrade_program_changed', '程序文件与发布身份记录不符。')
    try:
        os.makedirs(native_io_path(destination.parent), exist_ok=True)
    except OSError:
        _refuse('upgrade_program_read', '程序文件无法复制。')
    number, written = hashlib.sha256(), 0
    try:
        with open(native_io_path(source), 'rb') as reader, \
                open(native_io_path(destination), 'wb') as writer:
            for block in iter(lambda: reader.read(1 << 20), b''):
                number.update(block)
                writer.write(block)
                written += len(block)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError:
        _refuse('upgrade_program_read', '程序文件无法复制。')
    if written != row['bytes'] or number.hexdigest() != row['sha256']:
        _refuse('upgrade_program_changed', '程序文件与发布身份记录不符。')


def verify(directory, published):
    """Re-prove one program directory against one published identity, file by file."""
    directory = Path(directory)
    if _reparse(directory) or not os.path.isdir(native_io_path(directory)):
        _refuse('upgrade_program_missing', '程序副本已不存在，未能完成校验。')
    try:
        if digest(directory/IDENTITY) != published['sha256']:
            _refuse('upgrade_program_changed', '程序副本的发布身份记录与预期不符。')
    except AssistantError:
        raise
    except OSError:
        _refuse('upgrade_program_missing', '程序副本缺少发布身份记录。')
    for row in published['files']:
        target = directory.joinpath(*parts(row['path']))
        try:
            details = os.lstat(native_io_path(target))
        except OSError:
            _refuse('upgrade_program_missing', '程序副本缺少已发布的文件。')
        if (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)
                or details.st_size != row['bytes']):
            _refuse('upgrade_program_changed', '程序副本的文件与发布身份记录不符。')
        if digest(target) != row['sha256']:
            _refuse('upgrade_program_changed', '程序副本的文件与发布身份记录不符。')
    return directory


def plan_restore(previous, current):
    """What a rollback writes, what it removes, and what it must leave alone."""
    older, newer = paths(previous), paths(current)
    write = [older[name] for name in sorted(older)]
    remove = sorted(name for name in newer if name not in older)
    keep = sorted(name for name in newer if name in older)
    return {'write': write, 'remove': remove, 'keep': keep}


def apply_plan(plan, directory, app):
    """Write one already-persisted plan over a program directory and prove the result.

    The plan is the record's own copy of both published documents, so a half-written directory
    is finished from the record instead of from whatever the directory happens to contain.
    """
    directory, app = Path(directory), Path(app)
    for row in plan['write']:
        _copy(directory.joinpath(*parts(row['path'])), app.joinpath(*parts(row['path'])), row)
    removed = []
    for name in plan['remove']:
        target = app.joinpath(*parts(name))
        if _reparse(target):
            _refuse('upgrade_program_changed', '程序目录中存在链接，未回退。')
        try:
            os.unlink(native_io_path(target))
        except FileNotFoundError:
            continue
        except OSError:
            _refuse('upgrade_program_changed', '无法移除新版本独有的程序文件，未回退。')
        removed.append(name)
    # The identity document belongs to the plan as well, so an interrupted attempt is finished
    # from the record and never leaves a directory that claims to be the newer build.
    shutil.copyfile(native_io_path(directory/IDENTITY), native_io_path(app/IDENTITY))
    return {'written': len(plan['write']), 'removed': removed}


def verify_plan(plan, app, identity_sha256):
    """Prove one directory holds exactly the persisted write set and none of the removed paths."""
    app = Path(app)
    try:
        if digest(app/IDENTITY) != identity_sha256:
            _refuse('upgrade_program_changed', '程序目录的发布身份记录与回退目标不符。')
    except OSError:
        _refuse('upgrade_program_missing', '程序目录缺少发布身份记录。')
    for row in plan['write']:
        target = app.joinpath(*parts(row['path']))
        try:
            details = os.lstat(native_io_path(target))
        except OSError:
            _refuse('upgrade_program_missing', '程序目录缺少回退后的已发布文件。')
        if (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)
                or details.st_size != row['bytes'] or digest(target) != row['sha256']):
            _refuse('upgrade_program_changed', '程序目录的文件与回退目标清单不符。')
    for name in plan['remove']:
        if os.path.exists(native_io_path(app.joinpath(*parts(name)))):
            _refuse('upgrade_program_changed', '新版本独有的程序文件仍留在目录中，未回退。')
    return app


def restore(directory, app, *, previous, current):
    """Write a verified previous published set over a program directory, proving the result.

    Only files of the newer build's own verified document are ever removed, and only when the
    previous document does not publish them; a path in neither document is left untouched.
    """
    directory, app = Path(directory), Path(app)
    plan = plan_restore(previous, current)
    result = apply_plan(plan, directory, app)
    verify_plan(plan, app, previous['sha256'])
    return {'identity_sha256': previous['sha256'], 'written': result['written'],
            'removed': result['removed'], 'kept': len(plan['keep'])}
