"""Select which local data generation a restored installation reads; file state only.

A restored installation keeps its physical identity: the same root directory, the same
database cluster and port, and the same platform account directory. Only three addresses
move with the generation:

    original   generation=None       dbname=qiban                store=<root>/store
               credential_id=<installation>
    restored   generation=<32 hex>   dbname=qiban_restore_<id>   store=<root>/generations/<id>/store
               credential_id=<id>

`<root>/active-generation.json` is the only commit point, and the original generation has no
such file. It holds exactly format=1, installation, generation and manifest_sha256. Every
restored generation carries `generation.json` in its own directory with format=1,
installation, id and ready=True. All addresses are derived from the trusted root plus a
validated identifier, so no caller supplies a path, and a symlink, junction or other reparse
point anywhere in a derived path is refused instead of followed.

The commit point is written by one durable file replacement after the target generation's
ready manifest is verified. A commit holds `<root>/generation.lock`, which serializes its read,
comparison and write for one installation, so two commits cannot both pass the comparison. A
lock that is still held is reported as generation_busy instead of being waited on silently. The
lock file itself is bookkeeping, not data. That one replacement is not a transaction over the
database, the credentials or older generations, and it proves nothing until the following
end-to-end validation passes. No existing data, generation or earlier file is deleted here, and
the original generation is only re-selected through an explicit generation=null pointer written
by trusted maintenance code; this module offers no user-facing rollback, cleanup or re-key step.

The root is canonicalized rather than rejected, so an installation root that is itself reached
through a link stays usable, while every derived path below it is checked by inspection instead
of by a shell command. A process that rewrites the directory between such a check and the read
it guards is outside what this module can detect.

Refusals are always AssistantError with a stable code: generation_root_invalid,
generation_path, generation_pointer_invalid, generation_manifest_missing,
generation_manifest_invalid, generation_manifest_hash_mismatch, generation_installation_invalid,
generation_installation_mismatch, generation_id_invalid, generation_store_missing,
generation_prepare_conflict, generation_expected_invalid, generation_selection_stale,
generation_lock_failed, generation_busy and generation_write_failed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager, suppress
from pathlib import Path

from filelock import FileLock, Timeout

from companion_core.errors import AssistantError

POINTER = 'active-generation.json'
GENERATIONS = 'generations'
STORE = 'store'
MANIFEST = 'generation.json'
LOCK = 'generation.lock'
LOCK_TIMEOUT = 0
ORIGINAL_DATABASE = 'qiban'
RESTORED_DATABASE_PREFIX = 'qiban_restore_'
POINTER_FIELDS = frozenset({'format', 'installation', 'generation', 'manifest_sha256'})
MANIFEST_FIELDS = frozenset({'format', 'installation', 'id', 'ready'})
MANIFEST_OPTIONAL_FIELDS = frozenset({'version'})
IDENTITY_FIELDS = ('installation', 'generation')
IDENTIFIER = re.compile('[0-9a-f]{32}')
DIGEST = re.compile('[0-9a-f]{64}')
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
UNSAFE = '本机资料目录中存在指向其他位置的链接，请人工检查。'
FIELDS = '代次记录字段不符。'


def _reparse(path):
    """Symlinks and Windows junctions/reparse points, tested by path inspection only."""
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


def _root(root):
    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AssistantError('generation_root_invalid', '本机资料目录不可用。') from None
    if not resolved.is_dir():
        raise AssistantError('generation_root_invalid', '本机资料目录不可用。')
    return resolved


def _derived(root, *names):
    """One address derived from the trusted root; never followed out of it."""
    path = root
    for name in names:
        path = path/name
        if _reparse(path):
            raise AssistantError('generation_path', UNSAFE)
    if not path.resolve().is_relative_to(root):
        raise AssistantError('generation_path', UNSAFE)
    return path


def _identifier(value, code, message):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise AssistantError(code, message)
    return value


def _installation(installation):
    return _identifier(installation, 'generation_installation_invalid', '安装标识无效。')


def _generation(generation):
    return _identifier(generation, 'generation_id_invalid', '代次标识无效。')


def _format_one(value):
    return type(value) is int and value == 1


def _present(path, code, message):
    """Whether the entry exists; an access failure is an explicit error, never an absence."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        raise AssistantError(code, message) from None
    return True


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise AssistantError('generation_pointer_invalid', '代次选择记录无法读取。') from None


def _manifest(root, installation, generation):
    """Verified ready manifest of one restored generation and the digest of its bytes."""
    path = _derived(root, GENERATIONS, generation, MANIFEST)
    if not path.is_file():
        raise AssistantError('generation_manifest_missing', '恢复代次缺少就绪清单。')
    try:
        raw = path.read_bytes()
    except OSError:
        raise AssistantError('generation_manifest_invalid', '恢复代次就绪清单无法读取。') from None
    try:
        value = json.loads(raw.decode('utf8'))
    except ValueError:
        raise AssistantError('generation_manifest_invalid', '恢复代次就绪清单无效。') from None
    if (not isinstance(value, dict) or not MANIFEST_FIELDS <= set(value)
            or set(value) - MANIFEST_FIELDS - MANIFEST_OPTIONAL_FIELDS):
        raise AssistantError('generation_manifest_invalid', FIELDS)
    if not _format_one(value['format']) or value['ready'] is not True:
        raise AssistantError('generation_manifest_invalid', '恢复代次尚未就绪。')
    if not isinstance(value['installation'], str) or not IDENTIFIER.fullmatch(value['installation']):
        raise AssistantError('generation_manifest_invalid', FIELDS)
    if value['installation'] != installation:
        raise AssistantError('generation_installation_mismatch', '恢复代次属于另一台安装，已拒绝使用。')
    if value['id'] != generation:
        raise AssistantError('generation_manifest_invalid', '恢复代次就绪清单与所在目录不符。')
    return value, hashlib.sha256(raw).hexdigest()


def _store(root, *names):
    path = _derived(root, *names)
    if not path.is_dir():
        raise AssistantError('generation_store_missing', '代次资料目录缺失。')
    return path


def _descriptor(root, installation, generation=None, digest=None):
    if generation is None:
        return {'format': 1, 'installation': installation, 'generation': None,
                'database': ORIGINAL_DATABASE, 'store': str(_derived(root, STORE)),
                'credential_id': installation, 'manifest_sha256': None, 'root': str(root)}
    return {'format': 1, 'installation': installation, 'generation': generation,
            'database': RESTORED_DATABASE_PREFIX+generation,
            'store': str(_derived(root, GENERATIONS, generation, STORE)),
            'credential_id': generation, 'manifest_sha256': digest, 'root': str(root)}


def _original(root, installation):
    _store(root, STORE)
    return _descriptor(root, installation)


def _replace(temporary, path):
    """The one replacement step of a commit; a test may fail on either side of it."""
    os.replace(temporary, path)


def _write(path, value):
    """Register one JSON file durably: fsync the temporary file, then replace it once."""
    temporary = path.with_name(f'{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp')
    if _reparse(temporary):
        raise AssistantError('generation_path', UNSAFE)
    try:
        try:
            with temporary.open('w', encoding='utf8') as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            _replace(temporary, path)
        except BaseException:
            with suppress(OSError):
                temporary.unlink()
            raise
    except OSError:
        raise AssistantError('generation_write_failed', '代次记录写入失败，请检查磁盘与目录权限。') from None


def load_selection(root, installation):
    """Current generation selection of one installation, derived from the root and its ids.

    A missing pointer means the original generation. An existing pointer is authoritative:
    an unreadable record, wrong fields, a missing manifest, a digest mismatch or a manifest of
    another installation raises AssistantError instead of selecting an original (empty) store.
    """
    root, installation = _root(root), _installation(installation)
    pointer = _derived(root, POINTER)
    if not _present(pointer, 'generation_pointer_invalid', '代次选择记录无法读取。'):
        return _original(root, installation)
    value = _read(pointer)
    if not isinstance(value, dict) or set(value) != POINTER_FIELDS:
        raise AssistantError('generation_pointer_invalid', FIELDS)
    if not _format_one(value['format']):
        raise AssistantError('generation_pointer_invalid', '代次选择记录版本不符。')
    if not isinstance(value['installation'], str) or not IDENTIFIER.fullmatch(value['installation']):
        raise AssistantError('generation_pointer_invalid', FIELDS)
    if value['installation'] != installation:
        raise AssistantError('generation_installation_mismatch', '代次选择记录属于另一台安装，已拒绝使用。')
    if value['generation'] is None:
        if value['manifest_sha256'] is not None:
            raise AssistantError('generation_pointer_invalid', FIELDS)
        return _original(root, installation)
    if not isinstance(value['generation'], str) or not IDENTIFIER.fullmatch(value['generation']):
        raise AssistantError('generation_pointer_invalid', FIELDS)
    if not isinstance(value['manifest_sha256'], str) or not DIGEST.fullmatch(value['manifest_sha256']):
        raise AssistantError('generation_pointer_invalid', FIELDS)
    generation = value['generation']
    _, digest = _manifest(root, installation, generation)
    if digest != value['manifest_sha256']:
        raise AssistantError('generation_manifest_hash_mismatch', '恢复代次就绪清单与代次选择记录不符。')
    _store(root, GENERATIONS, generation, STORE)
    return _descriptor(root, installation, generation, digest)


def prepare_selection(root, installation, generation):
    """Register the ready manifest of a restored generation that was already verified.

    Only trusted maintenance code calls this, after the generation's database, assets and key
    were all checked. Its directory and store must already exist: this never creates, replaces
    or deletes a directory and never touches the commit point. It takes no part in the commit
    lock, because it never changes the manifest of an already registered generation, and its own
    manifest is written with the same single replacement, so a concurrent commit sees either no
    manifest or the complete one. Registering the same manifest again is a no-op; any different
    existing manifest is refused rather than overwritten.
    """
    root, installation = _root(root), _installation(installation)
    generation = _generation(generation)
    directory = _derived(root, GENERATIONS, generation)
    if not directory.is_dir():
        raise AssistantError('generation_store_missing', '代次资料目录缺失。')
    _store(root, GENERATIONS, generation, STORE)
    manifest = {'format': 1, 'installation': installation, 'id': generation, 'ready': True}
    path = _derived(root, GENERATIONS, generation, MANIFEST)
    if _present(path, 'generation_manifest_invalid', '恢复代次就绪清单无法读取。'):
        existing, digest = _manifest(root, installation, generation)
        if existing != manifest:
            raise AssistantError('generation_prepare_conflict', '恢复代次就绪清单已存在且不同。')
        return _descriptor(root, installation, generation, digest)
    _write(path, manifest)
    _, digest = _manifest(root, installation, generation)
    return _descriptor(root, installation, generation, digest)


def _expectation(installation, expected_current):
    """The caller's view of the selection, restricted to its stable identity fields."""
    if not isinstance(expected_current, dict) or any(f not in expected_current for f in IDENTITY_FIELDS):
        raise AssistantError('generation_expected_invalid', '代次期望值必须包含安装与代次标识。')
    expected = {field: expected_current[field] for field in IDENTITY_FIELDS}
    _identifier(expected['installation'], 'generation_expected_invalid', '代次期望值的安装标识无效。')
    if expected['installation'] != installation:
        raise AssistantError('generation_installation_mismatch', '代次期望值属于另一台安装，已拒绝使用。')
    if expected['generation'] is not None:
        _identifier(expected['generation'], 'generation_expected_invalid', '代次期望值的代次标识无效。')
    return expected


@contextmanager
def _exclusive(root):
    """Serialize one installation's read, comparison and single pointer write."""
    lock = FileLock(str(_derived(root, LOCK)), timeout=LOCK_TIMEOUT)
    try:
        lock.acquire()
    except Timeout:
        raise AssistantError('generation_busy', '另一个代次提交正在进行，请稍后重试。') from None
    except OSError:
        raise AssistantError('generation_lock_failed', '代次提交锁不可用，请检查本机资料目录权限。') from None
    try:
        yield
    finally:
        lock.release()


def commit_selection(root, installation, generation, expected_current):
    """Point the installation at one prepared generation and return the selection read back.

    expected_current is the caller's own view of the selection. Only its stable identity fields
    installation and generation are used: their formats are checked, its installation must be
    this one, and a restored generation in it must be a plain identifier. Everything else in it
    is ignored, so a full descriptor from load_selection may be passed unchanged.

    A commit holds `<root>/generation.lock`, which serializes its read, comparison and single
    pointer write for this installation, so two commits cannot both pass the comparison: the
    second one either finds its generation already selected or fails as stale. A lock that is
    still held after LOCK_TIMEOUT is reported as generation_busy, never waited on silently.

    Reaching the requested generation is success. When it is already the selected generation
    with an intact manifest, the selection is returned without writing, so a caller whose
    response was lost may repeat the same call. Any other selection that differs from the
    expectation is refused as stale before any write. The target must already carry a ready
    manifest of this installation. The pointer write is one durable file replacement: an
    interruption before it leaves the old generation selected, an interruption after it leaves
    the new one. No existing data is ever deleted, and this one replacement is not a transaction
    over the database, credentials or older generations.
    """
    root, installation = _root(root), _installation(installation)
    if generation is not None:
        generation = _generation(generation)
    expected = _expectation(installation, expected_current)
    with _exclusive(root):
        current = load_selection(root, installation)
        if current['generation'] == generation:
            return current
        if any(expected[field] != current[field] for field in IDENTITY_FIELDS):
            raise AssistantError('generation_selection_stale', '当前代次已不是期望的代次，请重新确认。')
        digest = None
        if generation is not None:
            _, digest = _manifest(root, installation, generation)
            _store(root, GENERATIONS, generation, STORE)
        _write(_derived(root, POINTER), {'format': 1, 'installation': installation,
                                         'generation': generation, 'manifest_sha256': digest})
        return load_selection(root, installation)
