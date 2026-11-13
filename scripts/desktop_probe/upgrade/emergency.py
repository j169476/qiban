"""The way back when the new version's own maintenance backend cannot run at all.

The ordinary rollback is a cooperation. The new version is the only code that can read its own
schema, so it preserves the current data with its own writer and seals this machine's local
whitelist with the fallback generation's key; only then does the previous program, executed from
the maintenance carrier, apply the recovery rules and switch the pointer back. That cooperation
needs the new version's backend to work. When it cannot start (or cannot finish), no fresh
logical preservation can be produced and the ordinary path is not available.

What a person may do in exactly that situation is narrower than a rollback, and it is a decision
that was taken deliberately, not inferred here:

    seal     (previous program, executed from the recorded maintenance carrier)
             with every process stopped, the whole installation is copied once into
             ``<root>/upgrade/emergency/<transaction>``: the database cluster including its WAL,
             the stores, the generations, the pointer, the installation record, the local
             settings and the account folders - every root item except the ``upgrade`` area itself
             (the archive lives inside it), the transient ``logs``/``temp`` folders and the
             ``*.lock`` bookkeeping files. A symlink, junction or other reparse point anywhere
             refuses the whole archive instead of copying something outside the root; nothing
             here tries to out-guess an intentional tamperer. The directory tree is copied too,
             empty directories included: a stopped cluster needs ``pg_notify``, ``pg_replslot``
             and the other folders that hold no file to start at all. The credential slots the
             copied files cannot be read without are read from the Windows credential store,
             assembled in memory only, sealed with the existing per-user DPAPI call
             (``bili_assistant.browser_login_transport.crypt``) and written as one opaque blob;
             no clear text reaches the disk. Every copied file is proved by size and digest in a
             manifest that also names the directories, and the completion marker is the last file
             written.
    switch   (the same carrier)
             the recorded fallback generation is opened with the previous program's *own* schema,
             the existing recovery rules hold every platform and retire unfinished tasks, the
             pointer moves back, and the previous published file set is written over the installed
             directory. The plan is persisted before the pointer may move, so an interrupted
             switch is finished from the record - the same shape as the ordinary ``rolling-back``
             progress.

Four boundaries this module never crosses:

* it does not run the broken new version and it never opens the new schema. The seal touches the
  filesystem only; the switch opens exactly one store, the recorded fallback generation, with the
  profile of the program that is executing.
* the archive is fault material, an offline original of the faulty installation. It is **not** a
  logical preservation copy and no answer of this module may say it can be imported back. It is
  kept because it may be the only copy of the newest data.
* the local settings of the faulty new version are not carried back, and nothing here pretends
  they were. The fallback generation keeps its own owner record, its own application lock and its
  own local selections; only the recovery rules that keep platforms and unfinished tasks stopped
  are applied.
* an archive directory has no automatic deletion path. It is created here, listed in
  ``pins.json`` and skipped by every later cleanup, together with the faulty generation and the
  faulty build's program copy - entries ``cleanup.clean`` keeps even after the transaction that
  created them has finished.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

from credential_vault import VAULT, VaultCredentials
from install_windows import protect_directory
from portable_restore import _hold_session, _retire_tasks

from bili_assistant.browser_login_transport import crypt
from companion_core.errors import AssistantError
from companion_core.storage import utcnow
from qiban.desktop_profile import desktop_profile

from . import data, journal, locks, program, verification

FORMAT = 1
DIRECTORY = 'emergency'
MANIFEST = 'archive-manifest.json'
CREDENTIALS = 'archive-credentials.bin'
COMPLETE = 'complete.json'
PINS = 'pins.json'
KIND = 'fault-material'
# Copied: everything else under the root. Not copied: this archive's own area, the transient log
# and temp folders, and the lock files that only bookkeeping uses.
EXCLUDED = frozenset({'upgrade', 'logs', 'temp'})
LOCK_SUFFIX = '.lock'
MAX_PATH = 1000
MAX_RETAINED = 200
MAX_SMALL = 1 << 22
MAX_SEALED = 1 << 24
DIGEST = journal.DIGEST
IDENTIFIER = journal.IDENTIFIER
RECEIPT_FIELDS = journal.RECEIPT_FIELDS
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
IMMEDIATE = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
MANIFEST_FIELDS = frozenset({'format', 'installation', 'transaction', 'count', 'bytes', 'files',
                            'directories'})
ROW_FIELDS = frozenset({'path', 'bytes', 'sha256'})
MARKER_FIELDS = frozenset({'format', 'installation', 'transaction', 'kind', 'at',
                           'manifest_sha256', 'count', 'bytes', 'credentials', 'generations',
                           'programs'})
CREDENTIAL_FIELDS = frozenset({'slots', 'bytes', 'sha256'})
PINS_FIELDS = frozenset({'format', 'installation', 'at', 'archives'})
PIN_FIELDS = frozenset({'transaction', 'kind', 'at', 'manifest_sha256', 'count', 'bytes',
                        'generations', 'programs'})
NOTE_SEALED = ('封存是本机故障资料的原件离线复制，不是已经可以导入的逻辑保全；'
               '故障代次与它所需的凭据已按封存记录保留，普通清理会跳过它们。')
NOTE_SETTINGS = ('紧急回退只恢复旧版本自己的资料与设置：故障新版最后修改的本机设置没有保留，'
                 '所有平台与未完成任务已按恢复规则保持停止。')
NOTE_RETAINED = ('程序目录里还有新版本留下、两份发布清单都没有记录的文件；它们没有被删除，'
                 '旧版本可能因此无法正常运行，请在人工确认后处理。')


def _refuse(code, message):
    raise AssistantError(code, message)


def _root(root):
    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        _refuse('upgrade_root', '本机资料目录不可用，未继续维护操作。')
    if not resolved.is_dir() or not (resolved/'installation.json').is_file():
        _refuse('upgrade_root', '本机资料目录不可用，未继续维护操作。')
    return resolved


def _app(app):
    try:
        resolved = Path(app).resolve(strict=True)
    except (OSError, RuntimeError):
        _refuse('upgrade_program_missing', '程序目录不可用，未继续维护操作。')
    if not resolved.is_dir():
        _refuse('upgrade_program_missing', '程序目录不可用，未继续维护操作。')
    return resolved


def _running():
    """The same GUI marker check every other verb uses; imported late so the modules stay acyclic."""
    from . import service

    return service.running()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _file_digest(path):
    number = hashlib.sha256()
    try:
        with open(program.native_io_path(path), 'rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                number.update(block)
    except OSError:
        _refuse('upgrade_emergency_state', '紧急封存记录无法读取，未继续回退。')
    return number.hexdigest()


def _reparse(path):
    try:
        details = os.lstat(program.native_io_path(path))
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


def _parts(name):
    """A relative archive path, split for a safe join; the rule the program manifests also use."""
    if (not isinstance(name, str) or not 0 < len(name) <= MAX_PATH or name.startswith('/')
            or '\\' in name or ':' in name):
        _refuse('upgrade_emergency_path', '紧急封存的文件路径无效，未做紧急封存。')
    pieces = name.split('/')
    if any(piece in ('', '.', '..') for piece in pieces):
        _refuse('upgrade_emergency_path', '紧急封存的文件路径无效，未做紧急封存。')
    return pieces


def directory(root, *, create=False):
    """`<root>/upgrade/emergency`: the archives live beside the record and are owned like it."""
    area = journal.directory(root)/DIRECTORY
    if not create:
        return area
    native = program.native_io_path(area)
    if os.path.lexists(native) and (_reparse(area) or not os.path.isdir(native)):
        _refuse('upgrade_emergency_path', '紧急封存目录不可用，未继续维护操作。')
    try:
        os.makedirs(native, exist_ok=True)
        protect_directory(area)
    except Exception:
        _refuse('upgrade_emergency_permissions', '无法设置紧急封存目录权限，未继续维护操作。')
    return area


def archive_path(root, transaction):
    """`<root>/upgrade/emergency/<transaction>`; the name is derived, never a caller's path."""
    if not isinstance(transaction, str) or not IDENTIFIER.fullmatch(transaction):
        _refuse('upgrade_emergency_state', '事务标识无效，未继续维护操作。')
    return journal.directory(root)/DIRECTORY/transaction


def credentials_binding(installation_id, transaction):
    """The per-archive DPAPI entropy: this installation and this transaction, nothing secret."""
    context = {'kind': 'qiban-upgrade-emergency', 'installation': installation_id,
               'transaction': transaction}
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode('utf8')).digest()


# -- what is copied ---------------------------------------------------------


def _entries(folder):
    try:
        return sorted(os.scandir(program.native_io_path(folder)), key=lambda item: item.name)
    except OSError:
        _refuse('upgrade_emergency_copy', '本机资料目录无法读取，未做紧急封存。')


def _walk(folder, prefix, files, directories):
    """Every ordinary entry below one directory, refusing anything that could point outside."""
    for entry in _entries(folder):
        name = prefix+'/'+entry.name
        if entry.name.endswith(LOCK_SUFFIX):
            continue
        if _reparse(entry.path):
            _refuse('upgrade_emergency_path',
                    '本机资料目录中存在链接或联接，未做紧急封存，请人工检查。')
        if entry.is_dir(follow_symlinks=False):
            directories.append(name)
            _walk(Path(entry.path), name, files, directories)
        elif entry.is_file(follow_symlinks=False):
            files.append({'path': name, 'bytes': entry.stat(follow_symlinks=False).st_size})
        else:
            _refuse('upgrade_emergency_path', '本机资料目录中存在特殊条目，未做紧急封存。')
    return files, directories


def contents(root):
    """Every ordinary file *and* directory the archive copies, relative to the root.

    The whole root is copied - the cluster and its WAL, every store and generation, the pointer,
    the installation record, the local settings, the account and asset folders - except the
    ``upgrade`` area itself, the transient ``logs``/``temp`` folders and the ``*.lock``
    bookkeeping files, with the same refusal for any link or junction.

    The directory tree is part of the copy, not a by-product of the files in it. A stopped
    PostgreSQL cluster keeps directories that hold no file at all - ``pg_notify``,
    ``pg_replslot``, the ``pg_logical`` subfolders - and a copy whose tree is invented from the
    files alone cannot be started as a cluster when it is needed for a repair.
    """
    root, files, directories = Path(root), [], []
    for entry in _entries(root):
        if entry.name in EXCLUDED or entry.name.endswith(LOCK_SUFFIX):
            continue
        if _reparse(entry.path):
            _refuse('upgrade_emergency_path',
                    '本机资料目录中存在链接或联接，未做紧急封存，请人工检查。')
        if entry.is_dir(follow_symlinks=False):
            directories.append(entry.name)
            _walk(Path(entry.path), entry.name, files, directories)
        elif entry.is_file(follow_symlinks=False):
            files.append({'path': entry.name,
                          'bytes': entry.stat(follow_symlinks=False).st_size})
        else:
            _refuse('upgrade_emergency_path', '本机资料目录中存在特殊条目，未做紧急封存。')
    return (sorted(files, key=lambda row: row['path']), sorted(directories))


def _space(folder, required):
    """Refuse before writing anything when the archive plainly cannot fit beside the original."""
    try:
        free = shutil.disk_usage(program.native_io_path(folder)).free
    except OSError:
        _refuse('upgrade_emergency_space', '无法确认剩余磁盘空间，未做紧急封存。')
    if free < required+max(1 << 20, required//10):
        _refuse('upgrade_emergency_space',
                '磁盘剩余空间不足以封存本机资料，未做紧急封存；原指针与程序未改动。')


def _copy(source, destination, row):
    """Copy one file and prove the copy by size and digest before it may enter the manifest."""
    source, destination = Path(source), Path(destination)
    if _reparse(source) or not os.path.isfile(program.native_io_path(source)):
        _refuse('upgrade_emergency_path', '本机资料中存在链接或非普通文件，未做紧急封存。')
    try:
        os.makedirs(program.native_io_path(destination.parent), exist_ok=True)
    except OSError:
        _refuse('upgrade_emergency_copy', '无法建立紧急封存目录，原指针与程序未改动。')
    number, written = hashlib.sha256(), 0
    try:
        with open(program.native_io_path(source), 'rb') as reader, \
                open(program.native_io_path(destination), 'wb') as writer:
            for block in iter(lambda: reader.read(1 << 20), b''):
                number.update(block)
                writer.write(block)
                written += len(block)
            writer.flush()
            os.fsync(writer.fileno())
        again = hashlib.sha256()
        with open(program.native_io_path(destination), 'rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                again.update(block)
    except OSError:
        _refuse('upgrade_emergency_copy', '紧急封存复制失败，原指针与程序未改动。')
    if written != row['bytes'] or number.digest() != again.digest():
        _refuse('upgrade_emergency_copy', '紧急封存的复制结果与原件不一致，原指针与程序未改动。')
    return {'path': row['path'], 'bytes': written, 'sha256': number.hexdigest()}


# -- what is remembered -----------------------------------------------------


def _write_bytes(path, raw):
    """One durable file: fsync the temporary file, then replace the target once."""
    path = Path(path)
    temporary = path.with_name(path.name+'.'+secrets.token_hex(4)+'.tmp')
    native, temporary_native = (program.native_io_path(path),
                                program.native_io_path(temporary))
    try:
        with open(temporary_native, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_native, native)
    except OSError:
        try:
            os.unlink(temporary_native)
        except OSError:
            pass
        _refuse('upgrade_emergency_write', '紧急封存写入失败，请检查磁盘与目录权限。')
    return path


def _write_json(path, value):
    return _write_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf8'))


def _read_json(path, *, maximum):
    try:
        with open(program.native_io_path(path), 'rb') as stream:
            raw = stream.read(maximum+1)
    except FileNotFoundError:
        return None
    except OSError:
        _refuse('upgrade_emergency_state', '紧急封存记录无法读取，未继续维护操作。')
    if not 0 < len(raw) <= maximum:
        _refuse('upgrade_emergency_state', '紧急封存记录无效，未继续维护操作。')
    try:
        return json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError):
        _refuse('upgrade_emergency_state', '紧急封存记录无效，未继续维护操作。')


def _check_identity(value, *, fields, installation_id, transaction):
    """The fields every one of this archive's own small records must agree on."""
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['format']) is not int or value['format'] != FORMAT
            or value['installation'] != installation_id or value['transaction'] != transaction
            or value['kind'] != KIND or not isinstance(value['at'], str) or not value['at']
            or not isinstance(value['manifest_sha256'], str)
            or not DIGEST.fullmatch(value['manifest_sha256'])
            or type(value['count']) is not int or value['count'] <= 0
            or type(value['bytes']) is not int or value['bytes'] <= 0):
        _refuse('upgrade_emergency_state', '紧急封存的完成记录无效，未继续维护操作。')
    return value


def _check_mark(value, installation_id, transaction):
    _check_identity(value, fields=MARKER_FIELDS, installation_id=installation_id,
                    transaction=transaction)
    credentials = value['credentials']
    if (not isinstance(credentials, dict) or set(credentials) != CREDENTIAL_FIELDS
            or not isinstance(credentials['slots'], list) or not credentials['slots']
            or any(not isinstance(slot, str) or not IDENTIFIER.fullmatch(slot)
                   for slot in credentials['slots'])
            or type(credentials['bytes']) is not int or credentials['bytes'] <= 0
            or not isinstance(credentials['sha256'], str)
            or not DIGEST.fullmatch(credentials['sha256'])):
        _refuse('upgrade_emergency_state', '紧急封存的凭据记录无效，未继续维护操作。')
    _check_protection(value, '紧急封存的保护记录')
    return value


def _check_protection(value, label):
    """The generation ids and program identities an archive keeps alive."""
    generations, programs = value['generations'], value['programs']
    if not isinstance(generations, list) or not isinstance(programs, list):
        _refuse('upgrade_emergency_state', f'{label}无效，未继续维护操作。')
    for generation in generations:
        if generation is not None and (not isinstance(generation, str)
                                       or not IDENTIFIER.fullmatch(generation)):
            _refuse('upgrade_emergency_state', f'{label}无效，未继续维护操作。')
    for identity in programs:
        if not isinstance(identity, str) or not DIGEST.fullmatch(identity):
            _refuse('upgrade_emergency_state', f'{label}无效，未继续维护操作。')
    return generations, programs


def _check_pin(entry, installation_id):
    """One index row: the identity fields a completed archive is known by, plus what it keeps."""
    if (not isinstance(entry, dict) or set(entry) != PIN_FIELDS
            or not isinstance(entry['transaction'], str)
            or not IDENTIFIER.fullmatch(entry['transaction'])
            or entry['kind'] != KIND or not isinstance(entry['at'], str) or not entry['at']
            or not isinstance(entry['manifest_sha256'], str)
            or not DIGEST.fullmatch(entry['manifest_sha256'])
            or type(entry['count']) is not int or entry['count'] <= 0
            or type(entry['bytes']) is not int or entry['bytes'] <= 0):
        _refuse('upgrade_emergency_state', '紧急封存索引条目无效，未继续维护操作。')
    _check_protection(entry, '紧急封存索引的保护记录')
    return entry


def _load_pins(root, installation_id):
    """The small index of completed archives; None when this installation has never sealed one."""
    value = _read_json(directory(root)/PINS, maximum=MAX_SMALL)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != PINS_FIELDS
            or type(value['format']) is not int or value['format'] != FORMAT
            or value['installation'] != installation_id
            or not isinstance(value['at'], str) or not value['at']
            or not isinstance(value['archives'], list)):
        # A damaged index would hide the protection of a fault archive: refuse, never guess.
        _refuse('upgrade_emergency_state',
                '本机紧急封存索引无效，未继续维护操作，请人工检查。')
    for entry in value['archives']:
        _check_pin(entry, installation_id)
    return value


def pinned(root, installation_id):
    """The generations and program copies a completed archive still needs; read-only, one file.

    Deciding this costs one small read. The archive's copies are deliberately not re-hashed here:
    a pin is the record that a sealed original exists, and a later repair - not every cleanup -
    is what proves that original again.
    """
    value = _load_pins(root, installation_id)
    if value is None:
        return set(), set()
    generations = {generation for entry in value['archives']
                   for generation in entry['generations']}
    programs = {identity for entry in value['archives'] for identity in entry['programs']}
    return generations, programs


def _pin(root, installation_id, archive, *, generations, programs):
    """Record one completed archive before anything may be switched."""
    value = _load_pins(root, installation_id)
    at = utcnow()
    row = {'transaction': archive['transaction'], 'kind': KIND, 'at': at,
           'manifest_sha256': archive['manifest_sha256'], 'count': archive['count'],
           'bytes': archive['bytes'],
           'generations': sorted(generations, key=lambda item: item or ''),
           'programs': sorted(programs)}
    archives = [] if value is None else [
        entry for entry in value['archives'] if entry['transaction'] != row['transaction']]
    return _write_json(directory(root, create=True)/PINS,
                       {'format': FORMAT, 'installation': installation_id, 'at': at,
                        'archives': [*archives, row]})


def _archive_ready(root, record):
    """A complete archive for this transaction, when its marker and manifest still agree.

    Only small files are read - the marker, the manifest's own digest and the sealed blob's size
    and digest - so reusing a finished archive never costs a pass over a database cluster. When the
    record already carries its own ``emergency`` block that block is compared too; while the block
    is still missing (an interruption between the marker and the record write) the marker and the
    manifest are the whole proof, which is what makes a retry of the same transaction idempotent.
    """
    installation_id, transaction = record['installation'], record['id']
    target = archive_path(root, transaction)
    value = _read_json(target/COMPLETE, maximum=MAX_SMALL)
    if value is None:
        return None
    mark = _check_mark(value, installation_id, transaction)
    if record['emergency'] is not None:
        recorded = record['emergency']['archive']
        if (mark['manifest_sha256'] != recorded['manifest_sha256']
                or mark['count'] != recorded['count'] or mark['bytes'] != recorded['bytes']):
            _refuse('upgrade_emergency_state',
                    '紧急封存的完成记录与升级记录不符，未继续维护操作。')
    if _file_digest(target/MANIFEST) != mark['manifest_sha256']:
        _refuse('upgrade_emergency_state', '紧急封存的清单已被改动，未继续维护操作。')
    blob = target/CREDENTIALS
    try:
        size = os.lstat(program.native_io_path(blob)).st_size
    except OSError:
        _refuse('upgrade_emergency_state', '紧急封存的凭据副本已不存在，未继续维护操作。')
    if size != mark['credentials']['bytes'] or _file_digest(blob) != mark['credentials']['sha256']:
        _refuse('upgrade_emergency_state', '紧急封存的凭据副本已改变，未继续维护操作。')
    return mark


# -- the seal --------------------------------------------------------------


def credential_slots(root, record):
    """Every slot this installation's own material needs: base, fallback, current and generations.

    A superset, not a guess: the installation's own slot, the recorded fallback's slot, the
    selected generation's slot, and each generation whose ready manifest is still on disk. A
    generation's slot is named after the generation, so this does not depend on the allocation
    record being readable either.
    """
    installation_id = record['installation']
    slots = {installation_id, record['origin']['selection']['credential_id']}
    try:
        slots.add(data.selected(root, installation_id)['credential_id'])
    except AssistantError:
        pass
    slots.update(_ready_generations(root))
    return sorted(slots)


def _ready_generations(root):
    """Generation directories carrying a ready manifest, derived from the root."""
    folder = Path(root)/'generations'
    if _reparse(folder) or not os.path.isdir(program.native_io_path(folder)):
        return set()
    found = set()
    for entry in _entries(folder):
        if (not IDENTIFIER.fullmatch(entry.name) or _reparse(entry.path)
                or not entry.is_dir(follow_symlinks=False)):
            continue
        if os.path.isfile(program.native_io_path(Path(entry.path)/'generation.json')):
            found.add(entry.name)
    return found


def _sealed(vault, slots, *, binding):
    """Seal the credential values with the existing per-user DPAPI call; nothing reaches a file.

    The values are assembled in memory only. The sealed blob is the one file written, and the
    clear text is dropped from this function's own scope as soon as it has been sealed.
    """
    values = {}
    for slot in slots:
        try:
            raw = vault.get_password(VAULT, slot)
        except Exception:
            _refuse('upgrade_emergency_vault', '本机凭据库不可用，未做紧急封存。')
        if not isinstance(raw, str) or not raw:
            _refuse('upgrade_emergency_vault',
                    '封存所需的凭据缺失，未做紧急封存；原指针与程序未改动。')
        values[slot] = raw
    body = json.dumps({'format': FORMAT, 'slots': values}, ensure_ascii=False,
                      sort_keys=True).encode('utf8')
    try:
        sealed = crypt(body, binding)
    except Exception:
        sealed = None
    finally:
        del body, values
    if not isinstance(sealed, bytes) or not 0 < len(sealed) <= MAX_SEALED:
        _refuse('upgrade_emergency_credentials',
                '本机用户加密不可用，未做紧急封存；原指针与程序未改动。')
    return sealed


def _recreate(target):
    """Create this transaction's archive directory, dropping its own unfinished files first."""
    target = Path(target)
    native = program.native_io_path(target)
    if os.path.lexists(native):
        if _reparse(target) or not os.path.isdir(native):
            _refuse('upgrade_emergency_path', '紧急封存位置不是普通目录，未做紧急封存。')
        # The unfinished tree of this same transaction may hold files deeper than MAX_PATH, so the
        # removal has to go through the extended form as well.
        shutil.rmtree(native, ignore_errors=True)
        if os.path.lexists(native):
            _refuse('upgrade_emergency_copy',
                    '上一次未完成的紧急封存文件未能清除，未做紧急封存。')
    try:
        os.mkdir(native)
        protect_directory(target)
    except Exception:
        _refuse('upgrade_emergency_permissions', '无法建立紧急封存目录，未做紧急封存。')


def _cluster_stopped(root, library):
    """Prove the cluster is stopped, by the port and by the payload's own ``pg_ctl status``.

    No process is ever killed here: a server that answers is a reason to stop and ask a person,
    and an answer this call cannot read is the same refusal.
    """
    binaries = data._binaries(library)
    database = Path(root)/'database'
    if not data.free_port(data.state(root)):
        _refuse('upgrade_emergency_running',
                '本机数据库仍在运行，请先在栖伴窗口中正常退出后再继续。')
    if not (database/'PG_VERSION').is_file():
        _refuse('upgrade_emergency_state', '本机数据库目录不完整，未做紧急封存。')
    try:
        result = subprocess.run([str(binaries/'pg_ctl.exe'), '-D', str(database), 'status'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30, creationflags=IMMEDIATE)
    except (OSError, subprocess.SubprocessError):
        _refuse('upgrade_emergency_stopped', '无法确认数据库是否已停止，未做紧急封存。')
    if result.returncode == 0:
        _refuse('upgrade_emergency_running',
                '本机数据库仍在运行，请先在栖伴窗口中正常退出后再继续。')
    if result.returncode != 3:
        _refuse('upgrade_emergency_stopped', '无法确认数据库是否已停止，未做紧急封存。')


def _carrier(library, record):
    """The executing program must be this transaction's recorded maintenance carrier."""
    published = program.document(library)
    if published['sha256'] != record['carrier']['identity_sha256']:
        _refuse('upgrade_executing_program', '当前执行程序不是这次升级记录的维护载体，未继续回退。')
    program.verify(library, published)
    return published


def seal(root, *, library, vault=None):
    """Copy the faulty installation out of the stopped root; the pointer and program stay put.

    This is the step that makes the way back honest: nothing is switched until one complete
    archive of the faulty material, its sealed credentials and its pin are on disk. A failure
    here leaves the pointer, the program directory and the transaction record exactly as they
    were, and a retry of the same transaction reuses a finished archive instead of copying again.
    """
    root, library = _root(root), _app(library)
    vault = vault if vault is not None else VaultCredentials()
    record = journal.load(root, required=True)
    _carrier(library, record)
    receipt = verification.authorize(record)
    with locks.exclusive(root, restore=True):
        if _running():
            _refuse('upgrade_running', '栖伴仍在运行，请先在窗口中正常退出后再继续。')
        record = journal.load(root, required=True)
        transaction, installation_id = record['id'], record['installation']
        if record['phase'] == 'emergency':
            mark = _archive_ready(root, record)
            if mark is None:
                _refuse('upgrade_emergency_state',
                        '这次升级的紧急封存没有完成，未继续维护操作，请人工检查。')
            return _report(record, mark, already=True)
        if record['phase'] not in ('staged', 'confirmed'):
            _refuse('upgrade_state', '这次升级不是可以紧急封存的状态，未做任何改动。')
        _cluster_stopped(root, library)
        generations = sorted([record['clone']['generation']], key=lambda item: item or '')
        programs = sorted([record['target']['identity_sha256']])
        mark = _archive_ready(root, record)
        if mark is None or (mark['generations'], mark['programs']) != (generations, programs):
            slots = credential_slots(root, record)
            archive, mark = _assemble(
                root, directory(root, create=True)/transaction, contents(root), slots=slots,
                sealed=_sealed(vault, slots, binding=credentials_binding(installation_id,
                                                                         transaction)),
                installation_id=installation_id, transaction=transaction,
                generations=generations, programs=programs)
        else:
            # An archive this same transaction already finished is reused, not copied again.
            archive = {'transaction': transaction, 'manifest_sha256': mark['manifest_sha256'],
                       'count': mark['count'], 'bytes': mark['bytes']}
        _pin(root, installation_id, archive, generations=mark['generations'],
             programs=mark['programs'])
        record = journal.advance(root, journal.load(root, required=True), 'emergency',
                                 emergency={'at': utcnow(), 'receipt': receipt,
                                            'archive': archive, 'target': None,
                                            'progress': None})
    return _report(record, mark, already=False)


def _folders(target, directories):
    """Create the copied directory tree, so a directory that holds no file still exists."""
    for name in directories:
        try:
            os.makedirs(program.native_io_path(target.joinpath(*_parts(name))), exist_ok=True)
        except OSError:
            _refuse('upgrade_emergency_copy', '无法建立紧急封存目录，原指针与程序未改动。')


def _assemble(root, target, contents_value, *, slots, sealed, installation_id, transaction,
              generations, programs):
    """Write one archive and prove it; the completion marker is the last file written."""
    root, target = Path(root), Path(target)
    rows, directories = contents_value
    _space(target.parent, sum(row['bytes'] for row in rows))
    _recreate(target)
    _folders(target, directories)
    copied = [_copy(root.joinpath(*_parts(row['path'])),
                    target.joinpath(*_parts(row['path'])), row) for row in rows]
    _write_bytes(target/CREDENTIALS, sealed)
    copied.append({'path': CREDENTIALS, 'bytes': len(sealed), 'sha256': _digest(sealed)})
    copied.sort(key=lambda row: row['path'])
    manifest = {'format': FORMAT, 'installation': installation_id, 'transaction': transaction,
                'count': len(copied), 'bytes': sum(row['bytes'] for row in copied),
                'files': copied, 'directories': list(directories)}
    _write_json(target/MANIFEST, manifest)
    mark = {'format': FORMAT, 'installation': installation_id, 'transaction': transaction,
            'kind': KIND, 'at': utcnow(),
            'manifest_sha256': _file_digest(target/MANIFEST),
            'count': manifest['count'], 'bytes': manifest['bytes'],
            'credentials': {'slots': list(slots), 'bytes': len(sealed),
                            'sha256': _digest(sealed)},
            'generations': sorted(generations, key=lambda item: item or ''),
            'programs': sorted(programs)}
    # The completion marker is the last file written: an archive is only an archive once it says so.
    _write_json(target/COMPLETE, mark)
    return ({'transaction': transaction, 'manifest_sha256': mark['manifest_sha256'],
             'count': mark['count'], 'bytes': mark['bytes']}, mark)


def _report(record, mark, *, already):
    """What one seal did, in the terms a person needs: material, not a restorable copy."""
    archive = record['emergency']['archive']
    return {'ok': True, 'phase': record['phase'], 'transaction': record['id'],
            'already': already, 'archive': archive['transaction'],
            'files': archive['count'], 'bytes': archive['bytes'],
            'manifest_sha256': archive['manifest_sha256'], 'sealed_at': mark['at'],
            'slots': mark['credentials']['slots'],
            'material': KIND, 'restorable': False, 'note': NOTE_SEALED}


# -- the switch ------------------------------------------------------------


def _unknown(installed_app, previous):
    """Files the installed directory holds that neither published document names; kept, and said."""
    wanted = program.paths(previous)
    names, stack = [], [(Path(installed_app), '')]
    while stack:
        folder, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(program.native_io_path(folder)),
                             key=lambda item: item.name)
        except OSError:
            return {'count': None, 'paths': [], 'truncated': False}
        for entry in entries:
            # Only this entry's own name enters the record; a prefixed `entry.path` is I/O only.
            name = prefix+entry.name
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), name+'/'))
            elif (entry.is_file(follow_symlinks=False) and name not in wanted
                  and name != program.IDENTITY):
                names.append(name)
    names.sort()
    return {'count': len(names), 'paths': names[:MAX_RETAINED],
            'truncated': len(names) > MAX_RETAINED}


def _plan(root, record, previous, installed_app):
    """The file plan, persisted before the pointer may move.

    The newer build's own ``qiban-payload.json`` is used when it is still complete - its bytes are
    never checked against the installed files, because this way back exists precisely for an
    installation whose code is damaged. Without that document no directory is guessed at: only
    the previous published set is written, and the files neither document names are recorded as
    retained.
    """
    try:
        target = program.document(installed_app)
    except AssistantError:
        target = None
    retained = (_unknown(installed_app, previous) if target is None
                else {'count': 0, 'paths': [], 'truncated': False})
    progress = {'at': utcnow(),
                'pointer_before': data.selection_generation(root, record['installation']),
                'pointer_switched': False,
                'previous_files': previous['files'],
                'target_files': previous['files'] if target is None else target['files'],
                'previous_identity': previous['sha256'],
                'target_identity': record['target']['identity_sha256']}
    step = {'identity_sha256': None if target is None else target['sha256'],
            'files': 0 if target is None else len(target['files']),
            'retained': retained}
    return progress, step


def _recorded(root, record, *, progress=None, target=None):
    """Move the emergency part of one record forward by exactly one step."""
    current = journal.load(root, required=True)
    if current['id'] != record['id'] or current['phase'] != 'emergency-switching':
        _refuse('upgrade_state', '紧急回退记录在切换期间已变化。')
    emergency = {**current['emergency'],
                 'progress': current['emergency']['progress'] if progress is None else progress}
    if target is not None:
        emergency['target'] = target
    return journal.advance(root, current, 'emergency-switching', emergency=emergency)


def _switch(root, record, library, vault):
    """Hold every platform on the fallback and move the pointer back; the old schema only.

    The fallback generation is opened with the executing program's own profile and with its own
    material - its owner record, its application lock and its local selections are never
    rewritten here, because the faulty new version's settings are not this machine's settings any
    more and are not pretended to have survived.
    """
    installation_id = record['installation']
    fallback = record['origin']['selection']
    existing = data.selection_generation(root, installation_id)
    if existing == fallback['generation']:
        # The pointer commit succeeded before the progress write. The recovery rules ran before
        # that commit, so they are not applied a second time (platform holds are not no-ops).
        actual = data.selected(root, installation_id)
        if any(actual[field] != fallback[field] for field in (
                'generation', 'database', 'credential_id', 'manifest_sha256')):
            _refuse('upgrade_state', '已切回的资料与记录中的回退目标不一致。')
        _recorded(root, record, progress={**record['emergency']['progress'], 'at': utcnow(),
                                          'pointer_switched': True})
        return
    if existing != record['emergency']['progress']['pointer_before']:
        _refuse('upgrade_state', '当前代次已不是回退开始时的代次，未回退。')
    with data.session(root, library, vault=vault, profile=desktop_profile(),
                      selection=fallback) as (store, _, _, _, descriptor):
        if descriptor['generation'] != fallback['generation']:
            _refuse('upgrade_state', '回退目标代次与升级记录不符，未回退。')
        with store.db() as db:
            _hold_session(store, 'upgrade-'+record['id'], fallback['generation'], db)
            _retire_tasks(store, db)
    current = data.selected(root, installation_id)
    data.commit_selection(root, installation_id, fallback['generation'], current)
    _recorded(root, record, progress={**record['emergency']['progress'], 'at': utcnow(),
                                      'pointer_switched': True})


def switch_back(root, *, installed_app, library, vault=None):
    """Bring the previous published build back over the installed directory.

    Every step is taken from the record, so a retry after a pointer commit or a half-written
    program directory finishes the same plan instead of judging a mixed directory.
    """
    root, library = _root(root), _app(library)
    installed_app = _app(installed_app)
    if Path(library) == Path(installed_app):
        _refuse('upgrade_executing_program',
                '紧急回退必须由维护载体执行，不能在正在被覆盖的程序目录里运行；未做任何改动。')
    vault = vault if vault is not None else VaultCredentials()
    record = journal.load(root, required=True)
    if record['phase'] == 'emergency-rolled-back':
        return _switched(record, None, already=True)
    if record['phase'] not in ('emergency', 'emergency-switching'):
        _refuse('upgrade_state', '这次升级还没有完成紧急封存，未做任何改动。')
    # The way back only exists over a finished archive. Proving that is one small read of the
    # completion marker, the manifest's own digest and the sealed blob - never a pass over the
    # database cluster, which is why a switch stays cheap even on a large installation.
    if _archive_ready(root, record) is None:
        _refuse('upgrade_emergency_state',
                '紧急封存的完成标记不存在，未开始回退，请先完成封存。')
    previous = _carrier(library, record)
    verification.authorize(record)
    with locks.exclusive(root, restore=True):
        if _running():
            _refuse('upgrade_running', '栖伴仍在运行，请先在窗口中正常退出后再继续。')
        record = journal.load(root, required=True)
        if record['phase'] == 'emergency':
            _cluster_stopped(root, library)
            progress, target = _plan(root, record, previous, installed_app)
            record = journal.advance(root, record, 'emergency-switching',
                                     emergency={**record['emergency'], 'target': target,
                                                'progress': progress})
        elif record['phase'] != 'emergency-switching':
            _refuse('upgrade_state', '紧急回退记录在切换期间已变化。')
        progress = record['emergency']['progress']
        if progress['previous_identity'] != record['carrier']['identity_sha256']:
            _refuse('upgrade_state', '紧急回退进度与维护载体不符，未继续回退。')
        if not progress['pointer_switched']:
            _switch(root, record, library, vault)
        plan = {'write': progress['previous_files'],
                'remove': sorted({row['path'] for row in progress['target_files']}
                                 - {row['path'] for row in progress['previous_files']})}
        program.apply_plan(plan, library, installed_app)
        program.verify_plan(plan, installed_app, progress['previous_identity'])
        record = journal.advance(root, journal.load(root, required=True), 'emergency-rolled-back')
    return _switched(record, plan, already=False)


def _switched(record, plan, *, already):
    """What one switch did, including what it did not bring back."""
    target = record['emergency']['target']
    retained = target['retained']
    notes = [NOTE_SETTINGS, NOTE_SEALED]
    if target['identity_sha256'] is None:
        notes.append(NOTE_RETAINED)
    return {'ok': True, 'phase': record['phase'], 'transaction': record['id'], 'already': already,
            'generation': record['origin']['selection']['generation'],
            'archive': record['emergency']['archive']['transaction'],
            'material': KIND, 'restorable': False,
            'written': None if plan is None else len(plan['write']),
            'removed': [] if plan is None else plan['remove'],
            'retained': retained,
            'program_files': 'complete' if target['identity_sha256'] is not None else 'partial',
            'notes': notes}
