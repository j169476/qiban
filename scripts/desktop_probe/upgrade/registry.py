"""Which generations and program copies this installation's own upgrades created.

Cleanup may only ever touch artifacts an upgrade transaction registered here, and only when the
entry carries the proof that the data is still recoverable *on its own*: the preservation copy
that the generation's own code wrote, the credential slot that decrypts it, the fingerprint it
was taken from and the generation manifest recorded at registration time. An entry without that
proof is never a deletion candidate, and a valid copy belonging to another generation can never
stand in for a missing one.

The registry is an allocation record, not a cache: an entry is written before the artifact it
names can exist (the clone's generation id is registered before the pointer may move), it carries
the owning installation and the transaction that created it, and its identity fields are never
rewritten to point somewhere else. Only the receipt block may be filled in later - by the code
that owns that generation, when it is still the current one.

The original generation is registered the same way (``generation`` is null, database ``qiban``,
store ``store``, slot = the installation's own), because D61 does not exempt a complete initial
database that nothing references any more.

Program copies keep their roles: the carrier an unfinished transaction executes from, the
retained previous version, and the freshly confirmed target. Retention may only drop an entry no
unfinished transaction references and that has no role left.
"""
from __future__ import annotations

import json
import os

from companion_core.errors import AssistantError
from companion_core.storage import utcnow

from . import journal

FORMAT = 1
IDENTIFIER = journal.IDENTIFIER
DIGEST = journal.DIGEST
FIELDS = frozenset({'format', 'installation', 'at', 'generations', 'programs'})
GENERATION_FIELDS = frozenset({'generation', 'database', 'store', 'credential_id', 'transaction',
                               'registered_at', 'manifest_sha256', 'preservation',
                               'preservation_keyref', 'source_fingerprint', 'receipt_at'})
RECEIPT_FIELDS = frozenset({'name', 'sha256', 'bytes'})
PROGRAM_FIELDS = frozenset({'identity_sha256', 'count', 'bytes', 'transaction', 'registered_at',
                            'roles'})
ROLES = frozenset({'carrier', 'previous', 'target'})


def _refuse(code, message):
    raise AssistantError(code, message)


def path(root):
    return journal.directory(root)/journal.REGISTRY


def empty(installation):
    return {'format': FORMAT, 'installation': installation, 'at': utcnow(),
            'generations': [], 'programs': []}


def _check_generation(entry, installation):
    if not isinstance(entry, dict) or set(entry) != GENERATION_FIELDS:
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次条目字段不符。')
    generation = entry['generation']
    if generation is None:
        if (entry['database'] != 'qiban' or entry['store'] != 'store'
                or entry['credential_id'] != installation):
            _refuse('upgrade_registry_invalid', '升级分配记录中的原代次条目无效。')
        if entry['manifest_sha256'] is not None:
            _refuse('upgrade_registry_invalid', '原代次不应带有代次就绪清单。')
    else:
        if (not isinstance(generation, str) or not IDENTIFIER.fullmatch(generation)
                or entry['database'] != 'qiban_restore_'+generation
                or entry['store'] != 'generations/'+generation+'/store'
                or entry['credential_id'] != generation):
            _refuse('upgrade_registry_invalid', '升级分配记录中的代次条目无效。')
        if entry['manifest_sha256'] is not None and (
                not isinstance(entry['manifest_sha256'], str)
                or not DIGEST.fullmatch(entry['manifest_sha256'])):
            _refuse('upgrade_registry_invalid', '升级分配记录中的代次就绪清单无效。')
    if not isinstance(entry['transaction'], str) or not IDENTIFIER.fullmatch(entry['transaction']):
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次来源事务无效。')
    if not isinstance(entry['registered_at'], str) or not entry['registered_at']:
        _refuse('upgrade_registry_invalid', '升级分配记录中的登记时间无效。')
    preservation = entry['preservation']
    if preservation is None:
        if entry['preservation_keyref'] is not None or entry['receipt_at'] is not None \
                or entry['source_fingerprint'] is not None:
            _refuse('upgrade_registry_invalid', '升级分配记录中的代次收据不完整。')
        return
    if not isinstance(preservation, dict) or set(preservation) != RECEIPT_FIELDS:
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次保全描述无效。')
    name = preservation['name']
    if (not isinstance(name, str) or not 0 < len(name) <= 200 or '/' in name or '\\' in name
            or ':' in name or not name.endswith('.ccbackup')):
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次保全名称无效。')
    if (not isinstance(preservation['sha256'], str) or not DIGEST.fullmatch(preservation['sha256'])
            or type(preservation['bytes']) is not int or preservation['bytes'] <= 0):
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次保全描述无效。')
    keyref = entry['preservation_keyref']
    if keyref != entry['credential_id']:
        _refuse('upgrade_registry_invalid', '代次保全必须由该代次自己的凭据槽解密。')
    if (not isinstance(entry['source_fingerprint'], str)
            or not DIGEST.fullmatch(entry['source_fingerprint'])):
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次保全来源指纹无效。')
    if not isinstance(entry['receipt_at'], str) or not entry['receipt_at']:
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次收据时间无效。')


def validate(value, installation):
    if not isinstance(value, dict) or set(value) != FIELDS:
        _refuse('upgrade_registry_invalid', '升级分配记录字段不符。')
    if type(value['format']) is not int or value['format'] != FORMAT:
        _refuse('upgrade_registry_invalid', '升级分配记录版本不符。')
    if value['installation'] != installation:
        _refuse('upgrade_registry_invalid', '升级分配记录属于另一台安装，已拒绝使用。')
    for entry in value['generations']:
        _check_generation(entry, installation)
    for entry in value['programs']:
        if (not isinstance(entry, dict) or set(entry) != PROGRAM_FIELDS
                or not isinstance(entry['identity_sha256'], str)
                or not DIGEST.fullmatch(entry['identity_sha256'])
                or type(entry['count']) is not int or entry['count'] <= 0
                or type(entry['bytes']) is not int or entry['bytes'] <= 0
                or not isinstance(entry['roles'], list) or not entry['roles']
                or not set(entry['roles']) <= ROLES
                or not isinstance(entry['transaction'], str)
                or not IDENTIFIER.fullmatch(entry['transaction'])):
            _refuse('upgrade_registry_invalid', '升级分配记录中的程序副本条目无效。')
    if len({entry['generation'] for entry in value['generations']}) != len(value['generations']):
        _refuse('upgrade_registry_invalid', '升级分配记录中的代次重复。')
    if len({entry['identity_sha256'] for entry in value['programs']}) != len(value['programs']):
        _refuse('upgrade_registry_invalid', '升级分配记录中的程序副本重复。')
    return value


def load(root, installation):
    file = path(root)
    try:
        raw = file.read_bytes()
    except FileNotFoundError:
        return empty(installation)
    except OSError:
        _refuse('upgrade_registry_invalid', '升级分配记录无法读取。')
    try:
        return validate(json.loads(raw.decode('utf8')), installation)
    except (ValueError, UnicodeError):
        _refuse('upgrade_registry_invalid', '升级分配记录无效。')


def save(root, value):
    validate(value, value['installation'])
    journal.directory(root, create=True)
    target = path(root)
    temporary = target.with_name(target.name+'.tmp')
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
        _refuse('upgrade_write', '升级分配记录写入失败，请检查磁盘与目录权限。')
    return load(root, value['installation'])


def _selection_fields(selection, installation):
    """The entry identity of one journal selection record."""
    generation = selection['generation']
    return {'generation': generation, 'database': selection['database'],
            'store': selection['store'],
            'credential_id': selection['credential_id'] if generation is not None else installation}


def register_generation(root, installation, selection, transaction, *, manifest_sha256=None,
                        preservation=None, source_fingerprint=None):
    """Register one generation (or the original) before cleanup could ever consider it.

    A receipt is only accepted for the generation it describes, decrypted by that generation's
    own slot; callers pass exactly what the owning code verified.
    """
    value = load(root, installation)
    entry = {**_selection_fields(selection, installation), 'transaction': transaction,
             'registered_at': utcnow(), 'manifest_sha256': manifest_sha256,
             'preservation': None if preservation is None else
             {'name': preservation['name'], 'sha256': preservation['sha256'],
              'bytes': preservation['bytes']},
             'preservation_keyref': None, 'source_fingerprint': None, 'receipt_at': None}
    if preservation is not None:
        entry.update({'preservation_keyref': entry['credential_id'],
                      'source_fingerprint': source_fingerprint, 'receipt_at': utcnow()})
    for existing in value['generations']:
        if existing['generation'] != entry['generation']:
            continue
        merged = dict(existing)
        if manifest_sha256 is not None:
            if merged['manifest_sha256'] not in (None, manifest_sha256):
                _refuse('upgrade_registry_invalid', '同一个代次已登记过另一份就绪清单。')
            merged['manifest_sha256'] = manifest_sha256
        if preservation is not None:
            merged.update({'preservation': entry['preservation'],
                           'preservation_keyref': entry['preservation_keyref'],
                           'source_fingerprint': entry['source_fingerprint'],
                           'receipt_at': entry['receipt_at']})
        if merged == existing:
            return value
        entries = [merged if item['generation'] == entry['generation'] else item
                   for item in value['generations']]
        return save(root, {**value, 'generations': entries})
    return save(root, {**value, 'generations': [*value['generations'], entry]})


def register_program(root, installation, published, transaction, role):
    """Record one program copy this transaction needs, unioning the roles that reference it."""
    if role not in ROLES:
        _refuse('upgrade_registry_invalid', '程序副本角色无效。')
    value = load(root, installation)
    entries, found = [], False
    for entry in value['programs']:
        if entry['identity_sha256'] != published['sha256']:
            entries.append(entry)
            continue
        found = True
        entries.append({**entry, 'roles': sorted(set(entry['roles']) | {role})})
    if not found:
        entries.append({'identity_sha256': published['sha256'], 'count': published['count'],
                        'bytes': published['bytes'], 'transaction': transaction,
                        'registered_at': utcnow(), 'roles': [role]})
    if entries == value['programs']:
        return value
    return save(root, {**value, 'programs': entries})


def drop_generation(root, installation, generation):
    value = load(root, installation)
    kept = [entry for entry in value['generations'] if entry['generation'] != generation]
    if len(kept) == len(value['generations']):
        return value
    return save(root, {**value, 'generations': kept})


def drop_program(root, installation, identity_sha256, role=None):
    """Remove one role; the entry disappears when no role is left. Unknown ids are a no-op."""
    value = load(root, installation)
    kept = []
    for entry in value['programs']:
        if entry['identity_sha256'] != identity_sha256:
            kept.append(entry)
            continue
        roles = sorted(set(entry['roles']) - ({role} if role else set(entry['roles'])))
        if roles:
            kept.append({**entry, 'roles': roles})
    if kept == value['programs']:
        return value
    return save(root, {**value, 'programs': kept})


def referenced(record):
    """Generation ids and program identities one journal record still needs."""
    generations, programs = set(), set()
    if record is None:
        return generations, programs
    for part in (record['plan'], record['clone']):
        if part is not None:
            generations.add(part['generation'])
    generations.add(record['origin']['selection']['generation'])
    programs.add(record['carrier']['identity_sha256'])
    programs.add(record['target']['identity_sha256'])
    programs.add(record['origin']['identity_sha256'])
    return generations, programs
