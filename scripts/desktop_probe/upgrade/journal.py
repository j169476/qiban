"""One durable record of an upgrade transaction; strict shape, strict phase rules.

An upgrade pairs one published program with one data generation, and no single filesystem step
can move both. The record - not the pointer - is the authority on what the transaction intends,
what it already did and what the next step is:

    prepared        preservation verified and the maintenance carrier proved file by file; the
                    old selection and the target program identity are recorded. Nothing moved.
    staging         the clone's id, database, store and credential slot are recorded *before*
                    the pointer may move.
    staged          the pointer names the clone; the old generation is untouched.
    rollback-ready  the new program (the only code that can read the new schema) preserved the
                    current data, encrypted this machine's local whitelist into the record and
                    left a receipt; the previous program may now switch back.
    rolling-back    the switch back is under way: both complete file manifests and whether the
                    pointer already moved are recorded, so a half-written program directory is
                    finished from the record instead of from a mixed directory.
    emergency       the previous program sealed the faulty installation offline - its own material,
                    the credential slots it cannot be read without and the manifest that proves
                    the copy - and pinned the faulty generation and build in ``pins.json``. Nothing
                    has been switched. This branch exists because the new version's own backend
                    cannot run, so no fresh logical preservation can be produced.
    emergency-switching
                    the way back is under way on that archive: the target plan and the same
                    pointer/progress facts as ``rolling-back`` are recorded before the pointer may
                    move, so an interrupted emergency switch is finished from the record.
    emergency-rolled-back
                    the previous build is back on the recorded fallback generation and the faulty
                    material stays sealed and pinned.
    confirmed       the new program opened and validated the clone; the pairing is confirmed and
                    older upgrade artifacts may be cleaned.
    rolled-back     the pointer is back on the recorded old selection and its files are back.
    aborted         a prepared/staging attempt was abandoned. A staging attempt that was
                    interrupted before the pointer moved is cleaned through the same step.

Phase rules are enforced here, not by callers: only legal successors are accepted, `plan`,
`clone`, `confirmed`, `rollback` and `progress` may only exist where they mean something, and a
record that cannot be legal is refused rather than repaired by guesswork. The ordinary rollback
branch and the emergency branch never share a record: a record carries `rollback` or `emergency`,
never both, and the ordinary path is unchanged by the existence of the other.

No absolute path, no key, no password and no chat content is stored. Stores are recorded as
paths derived from the installation root, the local whitelist of a rollback is stored only as
ciphertext, and the only identifiers are digests and 32-hex ids.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from install_windows import protect_directory

from companion_core.errors import AssistantError
from companion_core.storage import utcnow

FORMAT = 1
DIRECTORY = 'upgrade'
JOURNAL = 'transaction.json'
LOCK = 'lock'
PROGRAMS = 'programs'
REGISTRY = 'registry.json'
VERIFICATION = 'verification.json'
IDENTIFIER = re.compile('[0-9a-f]{32}')
DIGEST = re.compile('[0-9a-f]{64}')
PHASES = ('prepared', 'staging', 'staged', 'rollback-ready', 'rolling-back', 'confirmed',
          'rolled-back', 'aborted', 'emergency', 'emergency-switching', 'emergency-rolled-back')
OPEN_PHASES = frozenset({'prepared', 'staging', 'staged', 'rollback-ready', 'rolling-back',
                         'emergency', 'emergency-switching'})
SWITCHED_PHASES = frozenset({'staged', 'rollback-ready', 'rolling-back', 'confirmed', 'rolled-back',
                             'emergency', 'emergency-switching', 'emergency-rolled-back'})
TERMINAL_PHASES = frozenset({'confirmed', 'rolled-back', 'aborted', 'emergency-rolled-back'})
EMERGENCY_PHASES = frozenset({'emergency', 'emergency-switching', 'emergency-rolled-back'})
# Phase -> phases it may become. Anything else is refused instead of being repaired by guess.
SUCCESSORS = {
    'prepared': ('staging', 'aborted'),
    'staging': ('staged', 'aborted'),
    'staged': ('rollback-ready', 'confirmed', 'staged', 'emergency'),
    'rollback-ready': ('rolling-back', 'rollback-ready'),
    'rolling-back': ('rolling-back', 'rolled-back'),
    'confirmed': ('confirmed', 'rollback-ready', 'emergency'),
    'rolled-back': ('rolled-back',),
    'aborted': ('aborted',),
    'emergency': ('emergency', 'emergency-switching'),
    'emergency-switching': ('emergency-switching', 'emergency-rolled-back'),
    'emergency-rolled-back': ('emergency-rolled-back',),
}
RECORD_FIELDS = frozenset({'format', 'id', 'installation', 'phase', 'at', 'steps', 'origin',
                           'target', 'carrier', 'preservation', 'plan', 'clone', 'confirmed',
                           'rollback', 'cleanup', 'emergency'})
ORIGIN_FIELDS = frozenset({'identity_sha256', 'selection', 'data'})
TARGET_FIELDS = frozenset({'identity_sha256'})
CARRIER_FIELDS = frozenset({'identity_sha256', 'count', 'bytes'})
SELECTION_FIELDS = frozenset({'generation', 'database', 'store', 'credential_id', 'manifest_sha256'})
DATA_FIELDS = frozenset({'fingerprint', 'migrations_sha256'})
PRESERVATION_FIELDS = frozenset({'name', 'sha256', 'bytes', 'generation', 'store'})
PLAN_FIELDS = frozenset({'generation', 'database', 'store', 'credential_id'})
CLONE_FIELDS = frozenset({'generation', 'database', 'store', 'credential_id', 'manifest_sha256'})
CONFIRMED_FIELDS = frozenset({'identity_sha256', 'at', 'data'})
ROLLBACK_FIELDS = frozenset({'at', 'receipt', 'preservation', 'preserved_locally', 'settings',
                             'progress'})
RECEIPT_FIELDS = frozenset({'session', 'at'})
SETTINGS_FIELDS = frozenset({'format', 'keyref', 'identifier', 'ciphertext'})
PROGRESS_FIELDS = frozenset({'at', 'pointer_before', 'pointer_switched', 'previous_files',
                             'target_files', 'previous_identity', 'target_identity'})
FILE_ROW_FIELDS = frozenset({'path', 'bytes', 'sha256'})
EMERGENCY_FIELDS = frozenset({'at', 'receipt', 'archive', 'target', 'progress'})
ARCHIVE_FIELDS = frozenset({'transaction', 'manifest_sha256', 'count', 'bytes'})
TARGET_PLAN_FIELDS = frozenset({'identity_sha256', 'files', 'retained'})
RETAINED_FIELDS = frozenset({'count', 'paths', 'truncated'})
MAX_RETAINED = 200


def _refuse(code, message):
    raise AssistantError(code, message)


def directory(root, *, create=False):
    """`<root>/upgrade`: the transaction's own area, owned and protected like the root."""
    area = Path(root)/DIRECTORY
    if not create:
        return area
    if area.exists() and (area.is_symlink() or not area.is_dir()):
        _refuse('upgrade_path', '升级记录目录不可用，未继续维护操作。')
    try:
        area.mkdir(exist_ok=True)
        protect_directory(area)
    except Exception:
        _refuse('upgrade_directory_permissions', '无法设置升级记录目录权限，未继续维护操作。')
    return area


def _digest(value, field):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        _refuse('upgrade_journal_invalid', f'升级记录中的 {field} 无效。')
    return value


def _identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _refuse('upgrade_journal_invalid', f'升级记录中的 {field} 无效。')
    return value


def selection(descriptor):
    """The stable identity of one selected generation, derived from `load_selection`."""
    generation = descriptor['generation']
    return {'generation': generation, 'database': descriptor['database'],
            'store': 'store' if generation is None else 'generations/'+generation+'/store',
            'credential_id': descriptor['credential_id'],
            'manifest_sha256': descriptor['manifest_sha256'] if generation is not None else None}


def _check_selection(value):
    if not isinstance(value, dict) or set(value) != SELECTION_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的旧代次描述无效。')
    generation = value['generation']
    if generation is None:
        if value['database'] != 'qiban' or value['store'] != 'store' or value['manifest_sha256'] is not None:
            _refuse('upgrade_journal_invalid', '升级记录中的原代次描述无效。')
    else:
        _identifier(generation, '旧代次标识')
        if value['database'] != 'qiban_restore_'+generation:
            _refuse('upgrade_journal_invalid', '升级记录中的旧代次数据库无效。')
        if value['store'] != 'generations/'+generation+'/store':
            _refuse('upgrade_journal_invalid', '升级记录中的旧代次资料目录无效。')
        _digest(value['manifest_sha256'], '旧代次就绪清单')
    _identifier(value['credential_id'], '旧代次凭据槽')
    return value


def _check_data(value):
    if not isinstance(value, dict) or set(value) != DATA_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的资料指纹无效。')
    _digest(value['fingerprint'], '资料指纹')
    _digest(value['migrations_sha256'], '迁移集合')
    return value


def _check_preservation(value):
    if not isinstance(value, dict) or set(value) != PRESERVATION_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的保全备份无效。')
    name = value['name']
    if (not isinstance(name, str) or not 0 < len(name) <= 200 or '/' in name or '\\' in name
            or ':' in name or not name.endswith('.ccbackup')):
        _refuse('upgrade_journal_invalid', '升级记录中的保全备份名称无效。')
    _digest(value['sha256'], '保全备份摘要')
    if type(value['bytes']) is not int or value['bytes'] <= 0:
        _refuse('upgrade_journal_invalid', '升级记录中的保全备份大小无效。')
    generation = value['generation']
    if generation is not None:
        _identifier(generation, '保全备份所属代次')
    expected = 'store' if generation is None else 'generations/'+generation+'/store'
    if value['store'] != expected:
        _refuse('upgrade_journal_invalid', '升级记录中的保全备份目录无效。')
    return value


def _check_plan(value):
    if not isinstance(value, dict) or set(value) != PLAN_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的克隆计划无效。')
    _identifier(value['generation'], '克隆代次标识')
    if value['database'] != 'qiban_restore_'+value['generation']:
        _refuse('upgrade_journal_invalid', '升级记录中的克隆数据库无效。')
    if value['store'] != 'generations/'+value['generation']+'/store':
        _refuse('upgrade_journal_invalid', '升级记录中的克隆资料目录无效。')
    if value['credential_id'] != value['generation']:
        _refuse('upgrade_journal_invalid', '升级记录中的克隆凭据槽无效。')
    return value


def _check_clone(value):
    if not isinstance(value, dict) or set(value) != CLONE_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的新代次描述无效。')
    _check_plan({field: value[field] for field in PLAN_FIELDS})
    _digest(value['manifest_sha256'], '新代次就绪清单')
    return value


def _check_files(rows):
    if not isinstance(rows, list) or not rows:
        _refuse('upgrade_journal_invalid', '升级记录中的程序文件清单无效。')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != FILE_ROW_FIELDS:
            _refuse('upgrade_journal_invalid', '升级记录中的程序文件清单无效。')
        path = row['path']
        if (not isinstance(path, str) or not 0 < len(path) <= 400 or path.startswith('/')
                or '\\' in path or ':' in path or '..' in path.split('/') or path in seen):
            _refuse('upgrade_journal_invalid', '升级记录中的程序文件清单无效。')
        seen.add(path)
        if (type(row['bytes']) is not int or row['bytes'] < 0 or not isinstance(row['sha256'], str)
                or not DIGEST.fullmatch(row['sha256'])):
            _refuse('upgrade_journal_invalid', '升级记录中的程序文件清单无效。')
    return rows


def _check_settings(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != SETTINGS_FIELDS
            or type(value['format']) is not int or value['format'] != 1):
        _refuse('upgrade_journal_invalid', '升级记录中的本机设置收据无效。')
    _identifier(value['keyref'], '本机设置收据凭据槽')
    _identifier(value['identifier'], '本机设置收据标识')
    if not isinstance(value['ciphertext'], str) or not 0 < len(value['ciphertext']) <= 1 << 22:
        _refuse('upgrade_journal_invalid', '升级记录中的本机设置收据无效。')
    return value


def _check_progress(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != PROGRESS_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的回退进度无效。')
    if not isinstance(value['at'], str) or type(value['pointer_switched']) is not bool:
        _refuse('upgrade_journal_invalid', '升级记录中的回退进度无效。')
    for field in ('pointer_before',):
        if value[field] is not None:
            _identifier(value[field], '回退前代次')
    _digest(value['previous_identity'], '上一版程序身份')
    _digest(value['target_identity'], '目标程序身份')
    _check_files(value['previous_files'])
    _check_files(value['target_files'])
    return value


def _check_rollback(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != ROLLBACK_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的回滚信息无效。')
    receipt = value['receipt']
    if receipt is not None:
        if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS
                or not isinstance(receipt['session'], str) or not receipt['session']):
            _refuse('upgrade_journal_invalid', '升级记录中的回滚收据无效。')
    if receipt is None and value['preserved_locally'] is not True:
        _refuse('upgrade_journal_invalid', '升级记录中的回滚缺少保全来源。')
    _check_preservation(value['preservation'])
    _check_settings(value['settings'])
    _check_progress(value['progress'])
    return value


def _check_archive(value):
    """The offline archive one emergency seal produced, as the transaction remembers it."""
    if not isinstance(value, dict) or set(value) != ARCHIVE_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的紧急封存描述无效。')
    _identifier(value['transaction'], '紧急封存事务')
    _digest(value['manifest_sha256'], '紧急封存清单')
    if (type(value['count']) is not int or value['count'] <= 0
            or type(value['bytes']) is not int or value['bytes'] <= 0):
        _refuse('upgrade_journal_invalid', '升级记录中的紧急封存计数无效。')
    return value


def _check_retained(value):
    """Files the installed directory holds that neither published document names."""
    if (not isinstance(value, dict) or set(value) != RETAINED_FIELDS
            or type(value['truncated']) is not bool or not isinstance(value['paths'], list)
            or len(value['paths']) > MAX_RETAINED):
        _refuse('upgrade_journal_invalid', '升级记录中的保留文件清单无效。')
    if value['count'] is not None and (type(value['count']) is not int or value['count'] < 0):
        _refuse('upgrade_journal_invalid', '升级记录中的保留文件计数无效。')
    for name in value['paths']:
        if (not isinstance(name, str) or not 0 < len(name) <= 400 or name.startswith('/')
                or '\\' in name or ':' in name or '..' in name.split('/')):
            _refuse('upgrade_journal_invalid', '升级记录中的保留文件清单无效。')
    return value


def _check_target_plan(value):
    """The planned program file restore of one emergency switch."""
    if not isinstance(value, dict) or set(value) != TARGET_PLAN_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的紧急回退文件计划无效。')
    if value['identity_sha256'] is not None:
        _digest(value['identity_sha256'], '紧急回退目标清单')
    if type(value['files']) is not int or value['files'] < 0:
        _refuse('upgrade_journal_invalid', '升级记录中的紧急回退文件计划无效。')
    _check_retained(value['retained'])
    return value


def _check_emergency(value):
    """The emergency branch: the archive, the receipt, the plan and its own progress."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != EMERGENCY_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的紧急回退信息无效。')
    if not isinstance(value['at'], str) or not value['at']:
        _refuse('upgrade_journal_invalid', '升级记录中的紧急回退时间无效。')
    receipt = value['receipt']
    if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS
            or not isinstance(receipt['session'], str) or not receipt['session']):
        _refuse('upgrade_journal_invalid', '升级记录中的紧急回退凭据无效。')
    _check_archive(value['archive'])
    if value['target'] is not None:
        _check_target_plan(value['target'])
    _check_progress(value['progress'])
    return value


def validate(value):
    """Every field, every phase rule; a record that fails here is never used or replaced."""
    if isinstance(value, dict) and set(value) == RECORD_FIELDS-{'emergency'}:
        # A record written before the emergency branch existed is still readable; it simply has no
        # emergency part. Nothing is guessed about it beyond that absence.
        value = {**value, 'emergency': None}
    if not isinstance(value, dict) or set(value) != RECORD_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录字段不符。')
    if type(value['format']) is not int or value['format'] != FORMAT:
        _refuse('upgrade_journal_invalid', '升级记录版本不符。')
    _identifier(value['id'], '事务标识')
    _identifier(value['installation'], '安装标识')
    if value['phase'] not in PHASES:
        _refuse('upgrade_journal_invalid', '升级记录阶段无效。')
    if not isinstance(value['at'], str) or not value['at']:
        _refuse('upgrade_journal_invalid', '升级记录时间无效。')
    steps = value['steps']
    if not isinstance(steps, list) or not steps or any(step not in PHASES for step in steps):
        _refuse('upgrade_journal_invalid', '升级记录阶段轨迹无效。')
    if steps[-1] != value['phase']:
        _refuse('upgrade_journal_invalid', '升级记录阶段轨迹与当前阶段不符。')
    origin = value['origin']
    if not isinstance(origin, dict) or set(origin) != ORIGIN_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的来源程序无效。')
    _digest(origin['identity_sha256'], '来源程序身份')
    _check_selection(origin['selection'])
    _check_data(origin['data'])
    target = value['target']
    if not isinstance(target, dict) or set(target) != TARGET_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的目标程序无效。')
    _digest(target['identity_sha256'], '目标程序身份')
    if target['identity_sha256'] == origin['identity_sha256']:
        _refuse('upgrade_journal_invalid', '升级记录的目标程序与来源程序相同。')
    carrier = value['carrier']
    if not isinstance(carrier, dict) or set(carrier) != CARRIER_FIELDS:
        _refuse('upgrade_journal_invalid', '升级记录中的维护载体无效。')
    _digest(carrier['identity_sha256'], '载体身份')
    if carrier['identity_sha256'] != origin['identity_sha256']:
        _refuse('upgrade_journal_invalid', '升级记录中的维护载体与来源程序不符。')
    if type(carrier['count']) is not int or carrier['count'] <= 0 or type(carrier['bytes']) is not int:
        _refuse('upgrade_journal_invalid', '升级记录中的载体文件计数无效。')
    preservation = _check_preservation(value['preservation'])
    if preservation['generation'] != origin['selection']['generation']:
        _refuse('upgrade_journal_invalid', '升级记录中的保全备份不属于旧代次。')
    plan, clone = value['plan'], value['clone']
    if plan is not None:
        _check_plan(plan)
    if clone is not None:
        _check_clone(clone)
        if plan is not None and plan['generation'] != clone['generation']:
            _refuse('upgrade_journal_invalid', '升级记录中的克隆计划与克隆结果不一致。')
    confirmed = value['confirmed']
    if confirmed is not None:
        if not isinstance(confirmed, dict) or set(confirmed) != CONFIRMED_FIELDS:
            _refuse('upgrade_journal_invalid', '升级记录中的确认信息无效。')
        _digest(confirmed['identity_sha256'], '确认程序身份')
        if confirmed['identity_sha256'] != target['identity_sha256']:
            _refuse('upgrade_journal_invalid', '升级记录确认的程序不是目标程序。')
        _check_data(confirmed['data'])
    _check_rollback(value['rollback'])
    emergency = _check_emergency(value['emergency'])
    if emergency is not None and emergency['archive']['transaction'] != value['id']:
        _refuse('upgrade_journal_invalid', '升级记录中的紧急封存不属于这次事务。')
    cleanup = value['cleanup']
    if cleanup is not None:
        _check_cleanup(cleanup)
    _check_phase(value)
    return value


def _check_cleanup(value):
    fields = {'at', 'generations', 'programs', 'skipped', 'complete'}
    # Existing source-stage records predate durable failure details.
    if (not isinstance(value, dict) or set(value) not in (fields, fields | {'failed'})
            or not isinstance(value['generations'], list) or not isinstance(value['programs'], list)
            or not isinstance(value['skipped'], list) or type(value['complete']) is not bool
            or not isinstance(value.get('failed', []), list)):
        _refuse('upgrade_journal_invalid', '升级记录中的清理结果无效。')
    if value.get('failed') and value['complete']:
        _refuse('upgrade_journal_invalid', '清理仍有失败项，不能记录为完成。')


def _check_phase(value):
    """A phase may only carry the parts it implies; a missing or stray part is refused."""
    phase = value['phase']
    has_plan, has_clone = value['plan'] is not None, value['clone'] is not None
    confirmed, rollback = value['confirmed'], value['rollback']
    emergency = value['emergency']
    if phase == 'prepared' and (has_plan or has_clone or confirmed or rollback):
        _refuse('upgrade_journal_invalid', '准备阶段的升级记录不应包含克隆或确认信息。')
    if phase == 'staging' and (not has_plan or has_clone or confirmed):
        _refuse('upgrade_journal_invalid', '克隆阶段的升级记录缺少克隆计划。')
    if phase in ('staged', 'rollback-ready', 'rolling-back', 'confirmed') and not has_clone:
        _refuse('upgrade_journal_invalid', '已切换的升级记录缺少新代次描述。')
    if phase in ('confirmed',) and confirmed is None:
        _refuse('upgrade_journal_invalid', '确认阶段的升级记录缺少确认信息。')
    if phase in ('rollback-ready', 'rolling-back', 'rolled-back') and rollback is None:
        _refuse('upgrade_journal_invalid', '回退相关的升级记录缺少回滚信息。')
    if phase == 'rolling-back' and rollback['progress'] is None:
        _refuse('upgrade_journal_invalid', '回退进行中的升级记录缺少写入进度。')
    if phase == 'rolled-back':
        progress = rollback['progress']
        if progress is None or progress['pointer_switched'] is not True:
            _refuse('upgrade_journal_invalid', '回退完成的升级记录缺少指针对换证据。')
    if phase in ('prepared', 'staging', 'staged') and confirmed is not None:
        _refuse('upgrade_journal_invalid', '未确认的升级记录带有确认信息。')
    if phase in ('prepared', 'staging') and rollback is not None:
        _refuse('upgrade_journal_invalid', '未切换的升级记录带有回滚信息。')
    if phase == 'aborted' and (has_clone or confirmed):
        _refuse('upgrade_journal_invalid', '已放弃的升级记录不应包含克隆或确认信息。')
    # The emergency branch is its own record part: the two ways back never share one record.
    if emergency is not None and phase not in EMERGENCY_PHASES:
        _refuse('upgrade_journal_invalid', '普通阶段的升级记录不应包含紧急回退信息。')
    if phase in EMERGENCY_PHASES:
        if emergency is None:
            _refuse('upgrade_journal_invalid', '紧急回退的升级记录缺少紧急封存信息。')
        if rollback is not None:
            _refuse('upgrade_journal_invalid', '紧急回退的升级记录不应带有常规回滚信息。')
        if not has_clone:
            _refuse('upgrade_journal_invalid', '紧急回退的升级记录缺少新代次描述。')
        if phase == 'emergency' and (emergency['target'] is not None
                                     or emergency['progress'] is not None):
            _refuse('upgrade_journal_invalid', '刚封存的升级记录不应已有切换进度。')
        if phase in ('emergency-switching', 'emergency-rolled-back') and (
                emergency['target'] is None or emergency['progress'] is None):
            _refuse('upgrade_journal_invalid', '紧急回退进行中的升级记录缺少文件计划或进度。')
        if phase == 'emergency-rolled-back' and emergency['progress']['pointer_switched'] is not True:
            _refuse('upgrade_journal_invalid', '紧急回退完成的升级记录缺少指针对换证据。')


def path(root):
    return Path(root)/DIRECTORY/JOURNAL


def load(root, *, required=False):
    """The current record, strictly validated; None when this root has no upgrade."""
    file = path(root)
    try:
        raw = file.read_bytes()
    except FileNotFoundError:
        if required:
            _refuse('upgrade_journal_missing', '没有找到升级事务记录。')
        return None
    except OSError:
        _refuse('upgrade_journal_invalid', '升级事务记录无法读取。')
    try:
        value = json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError):
        _refuse('upgrade_journal_invalid', '升级事务记录无效。')
    return validate(value)


def save(root, value):
    """Replace the record once, durably, after validating it; returns the record."""
    validate(value)
    target = path(root)
    directory(root, create=True)
    temporary = target.with_name(target.name+'.'+secrets.token_hex(4)+'.tmp')
    try:
        with temporary.open('w', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        _refuse('upgrade_write', '升级事务记录写入失败，请检查磁盘与目录权限。')
    return load(root, required=True)


def advance(root, value, phase, **updates):
    """Move one record to its legal successor with the given fields replaced."""
    current = load(root, required=True)
    if current['id'] != value['id']:
        _refuse('upgrade_state', '升级事务记录在维护期间已变化。')
    if phase not in SUCCESSORS[current['phase']]:
        _refuse('upgrade_state', f'升级事务不能从 {current["phase"]} 直接进入 {phase}。')
    steps = [*current['steps'], phase]
    if steps[-2:] == [phase, phase]:
        steps = steps[:-1]
    return save(root, {**current, **updates, 'phase': phase, 'steps': steps})


def create(installation, origin, target, carrier, preservation, *, transaction=None):
    """A fresh `prepared` record: origin program, old selection, target and carrier in one file.

    The transaction id may be supplied by the caller so the maintenance carrier, the registry
    entry and the clone's derived generation id all name the same transaction from the start.
    """
    identifier = secrets.token_hex(16) if transaction is None else transaction
    return validate({'format': FORMAT, 'id': identifier, 'installation': installation,
                     'phase': 'prepared', 'at': utcnow(), 'steps': ['prepared'],
                     'origin': origin, 'target': target, 'carrier': carrier,
                     'preservation': preservation, 'plan': None, 'clone': None,
                     'confirmed': None, 'rollback': None, 'cleanup': None, 'emergency': None})


def reconcile_hint(value, current_generation):
    """Pure advice for a reader: does a `staging` record's commit already show in the pointer?"""
    if value is None or value['phase'] != 'staging' or value['plan'] is None:
        return None
    return 'staged' if current_generation == value['plan']['generation'] else None


def target_identity(value):
    return value['target']['identity_sha256'] if value is not None else None
