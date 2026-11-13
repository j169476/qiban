"""What an upgrade may ever delete: its own registered artifacts, with their own proof.

A generation is only a deletion candidate when the registry holds the preservation copy that
*that generation's own code* wrote, the receipt that proves it, the credential slot that decrypts
it and the fingerprint it was taken from - and when that copy still re-hashes and decrypts at
cleanup time. A valid copy belonging to another generation can never authorize the deletion of
this one, and an entry without a receipt is not a candidate at all.

The original generation is treated the same way (D61): a complete initial database is not kept
forever merely because it is the first one. It becomes a candidate only when neither the current
selection nor the most recent rollback target references it, and only with its own verified
preservation. What stays is the minimal index, the history folder that the retained copies are
enumerated through, and the credential slot that decrypts them - deleting a generation directory
wholesale would turn its preservation copies into unreadable bytes.

A generation is skipped without that being a defect only when something still protects it: the
current selection, the most recent rollback target, an unfinished transaction or a completed
emergency archive. Those are kept by design, and keeping them leaves ``complete: true``.

Every other generation is judged on its own proof, and a failed proof is a failure, not a skip. An
unprotected generation whose own credential slot, preservation copy, manifest or path no longer
verifies keeps its registry entry, is never deleted, and makes the record say ``complete: false``:
a damaged own preservation is exactly the case that must not be reported as "nothing to do". The
same holds for a candidate whose database or working assets could not be removed, so a later run
retries it. Unknown directories inside a store are written down in that generation's own result
instead of being guessed away.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path

from credential_vault import VaultCredentials

from companion_core.errors import AssistantError
from companion_core.storage import utcnow

from . import data, emergency, journal, locks, program, registry

KEEP = frozenset({'history'})
KEEP_ORIGINAL = frozenset({'history', 'index'})
WORKING = frozenset({'assets', 'cache', 'conflicts', 'runtime', 'index'})
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)


def _refuse(code, message):
    raise AssistantError(code, message)


def _reparse(path):
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


def _chain(root, store):
    """Every directory from the root down to one store; a link anywhere refuses the candidate."""
    root = Path(root)
    for path in (root, root/'generations', store.parent, store):
        if _reparse(path):
            _refuse('upgrade_path', '代次路径中存在链接或联接，未清理。')


def ownership(root, installation_id, entry):
    """Prove one candidate is this installation's own registered generation. Nothing is deleted."""
    root = Path(root)
    store = data.store_root(root, entry['store'])
    _chain(root, store)
    if not store.is_dir():
        _refuse('upgrade_state', '代次资料目录不存在，未清理。')
    generation = entry['generation']
    if generation is None:
        if (root/'installation.json').is_file() is False:
            _refuse('upgrade_state', '本机资料目录不可信，未清理。')
        return store
    directory = root/'generations'/generation
    if not directory.is_dir():
        _refuse('upgrade_state', '代次目录不存在，未清理。')
    try:

        import installation_generation

        manifest, digest = installation_generation._manifest(root, installation_id, generation)
        if digest != entry['manifest_sha256']:
            _refuse('upgrade_state', '代次就绪清单与登记不符，未清理。')
    except AssistantError:
        raise
    except Exception:
        _refuse('upgrade_state', '代次就绪清单无法核对，未清理。')
    if manifest['installation'] != installation_id or manifest['id'] != generation:
        _refuse('upgrade_state', '代次就绪清单不属于这台安装，未清理。')
    return store


def receipt_key(vault, installation_id, entry):
    """The slot this candidate's own preservation is decrypted with; missing proof is a refusal."""
    if entry['preservation'] is None or entry['preservation_keyref'] is None:
        _refuse('upgrade_preserve', '这个代次没有它自己的保全备份，不能清理。')
    if entry['preservation_keyref'] != entry['credential_id']:
        _refuse('upgrade_preserve', '代次保全不属于该代次自己的凭据槽，不能清理。')
    return data.slot_key(vault, installation_id, entry['preservation_keyref'])


def protection(generation, *, current, previous, referenced, sealed=frozenset()):
    """Why one generation must stay untouched, or ``None`` when nothing protects it.

    These refusals are the only ones that mean "keep it, nothing is wrong": the generation is
    still in use, or an emergency archive still needs it, so it is not a candidate and its
    retention says nothing about this cleanup.
    """
    if generation == current:
        return '当前选择的代次不能清理。'
    if generation == previous:
        return '最近一次回退目标不能清理。'
    if generation in referenced:
        return '未完成的升级事务仍在引用这个代次，不能清理。'
    if generation in sealed:
        return '本机紧急封存仍需要这个代次，不能清理。'
    return None


def check_candidate(root, installation_id, entry, *, vault, current, previous, referenced,
                    sealed=frozenset()):
    """All non-destructive proofs for one candidate; returns the store root and the slot key.

    Anything refused here is this generation's own proof failing, never a protection of somebody
    else's: the caller has already set the protected generations apart.
    """
    retained = protection(entry['generation'], current=current, previous=previous,
                          referenced=referenced, sealed=sealed)
    if retained is not None:
        _refuse('upgrade_state', retained)
    store = ownership(root, installation_id, entry)
    key = receipt_key(vault, installation_id, entry)
    data.verify_artifact(Path(root), {**entry['preservation'],
                                      'store': entry['store']}, key=key)
    return store, key


def _remove_working(store, keep):
    """Remove the working directories; keep the minimum and record anything unexpected."""
    removed, skipped = [], []
    try:
        entries = sorted(os.scandir(store), key=lambda item: item.name)
    except OSError:
        _refuse('upgrade_state', '代次资料目录无法读取，未清理。')
    for entry in entries:
        name = entry.name
        if name in keep:
            continue
        if name not in WORKING:
            skipped.append(f'未知工作目录未清理：{name}')
            continue
        if _reparse(entry.path) or not entry.is_dir(follow_symlinks=False):
            skipped.append(f'条目不按工作目录处理：{name}')
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        if Path(entry.path).exists():
            _refuse('upgrade_state', f'工作目录未能完全删除：{name}，保留登记以便重试。')
        removed.append(name)
    return removed, skipped


def clean_generation(root, installation_id, entry, *, vault, admin, current, previous, referenced,
                     sealed=frozenset()):
    """Drop one candidate; every proof is repeated immediately before the destructive step."""
    store, key = check_candidate(root, installation_id, entry, vault=vault, current=current,
                                 previous=previous, referenced=referenced, sealed=sealed)
    again = data.selection_generation(Path(root), installation_id)
    if again != current:
        _refuse('upgrade_state', '清理期间当前代次已改变，未清理这个代次。')
    data.drop_database(admin, entry['database'])
    keep = KEEP_ORIGINAL if entry['generation'] is None else KEEP
    removed, skipped = _remove_working(store, keep)
    data.verify_artifact(Path(root), {**entry['preservation'], 'store': entry['store']}, key=key)
    history = store/'history'
    if not history.is_dir():
        _refuse('upgrade_state', '清理后保全历史目录缺失，保留登记以便人工检查。')
    return {'generation': entry['generation'], 'database': entry['database'],
            'database_dropped': True, 'removed': removed, 'skipped': skipped}


def clean(root, *, state_value=None, credential=None, vault=None, app=None):
    """Clean this installation's finished upgrade artifacts; repeatable and honest about failures.

    Only a confirmed upgrade may be cleaned: a rolled-back or unfinished transaction still needs
    its artifacts, so it never triggers an automatic cleanup. The whole section is held for the
    duration, including the portable restore lock, so nothing can start a switch or a new
    preparation in the middle of it.

    Each registered generation is classified once, before any proof is taken on it: one still
    protected by the current selection, the most recent rollback target, an unfinished transaction
    or a completed emergency archive is a normal skip and keeping it changes nothing else. Every
    other generation must pass its own proofs, and failing them is a failure - the entry is kept,
    nothing is deleted and the record says ``complete: false``.

    A completed emergency seal pins the faulty generation and the faulty build's program copy in
    its own small index, so this cleanup keeps them even after the transaction that created them
    has finished. The sealed archive itself lives under ``upgrade/emergency/`` and has no
    automatic deletion path here: it is the fault material a later repair reads.
    """
    root = Path(root)
    vault = vault if vault is not None else VaultCredentials()
    report = {'generations': [], 'programs': [], 'skipped': [], 'failed': []}
    with locks.exclusive(root, restore=True):
        record = journal.load(root, required=True)
        if record['phase'] != 'confirmed':
            _refuse('upgrade_state', '只有已经确认的升级才会自动清理旧代次。')
        installation_id = record['installation']
        state_value = state_value if state_value is not None else data.state(root)
        if state_value.get('installation') != installation_id:
            _refuse('upgrade_state', '安装记录与升级事务不一致，未清理任何内容。')
        current = data.selection_generation(root, installation_id)
        previous = record['origin']['selection']['generation']
        referenced, referenced_programs = registry.referenced(record)
        sealed, sealed_programs = emergency.pinned(root, installation_id)
        value = registry.load(root, installation_id)
        candidates = []
        for entry in value['generations']:
            retained = protection(entry['generation'], current=current, previous=previous,
                                  referenced=referenced, sealed=sealed)
            if retained is not None:
                report['skipped'].append({'generation': entry['generation'],
                                          'code': 'upgrade_state', 'reason': retained})
                continue
            try:
                check_candidate(root, installation_id, entry, vault=vault, current=current,
                                previous=previous, referenced=referenced, sealed=sealed)
            except AssistantError as error:
                report['failed'].append({'generation': entry['generation'],
                                         'code': error.code, 'reason': str(error)})
                continue
            candidates.append(entry)
        if candidates:
            with contextlib.ExitStack() as stack:
                admin = None
                for entry in candidates:
                    try:
                        if admin is None:
                            admin = stack.enter_context(data.admin(root, vault=vault, app=app))
                        report['generations'].append(clean_generation(
                            root, installation_id, entry, vault=vault, admin=admin,
                            current=current, previous=previous, referenced=referenced,
                            sealed=sealed))
                    except AssistantError as error:
                        report['failed'].append({'generation': entry['generation'],
                                                 'code': error.code, 'reason': str(error)})
                        continue
                    registry.drop_generation(root, installation_id, entry['generation'])
        keep = record['carrier']['identity_sha256']
        for entry in registry.load(root, installation_id)['programs']:
            identity = entry['identity_sha256']
            if identity == keep or identity in referenced_programs:
                report['skipped'].append({'program': identity, 'code': 'upgrade_state',
                                          'reason': '仍被升级事务或上一版保留引用'})
                continue
            if identity in sealed_programs:
                report['skipped'].append({'program': identity, 'code': 'upgrade_state',
                                          'reason': '本机紧急封存仍需要这个程序副本，不能清理。'})
                continue
            directory = program.tree(root, identity)
            if not directory.exists():
                registry.drop_program(root, installation_id, identity)
                report['programs'].append({'identity_sha256': identity, 'removed': False,
                                           'reason': '程序副本已不存在'})
                continue
            try:
                mark = program.carrier_record(directory)
                if mark['installation'] != installation_id:
                    _refuse('upgrade_state', '程序副本属于另一台安装，未清理。')
                if mark['identity_sha256'] != identity:
                    _refuse('upgrade_state', '程序副本标记与目录名不符，未清理。')
                shutil.rmtree(program.native_io_path(directory), ignore_errors=True)
                if directory.exists():
                    _refuse('upgrade_state', '程序副本未能完全删除，保留登记以便重试。')
            except AssistantError as error:
                report['failed'].append({'program': identity, 'code': error.code,
                                         'reason': str(error)})
                continue
            registry.drop_program(root, installation_id, identity)
            report['programs'].append({'identity_sha256': identity, 'removed': True})
        complete = not report['failed']
        updated = journal.advance(root, journal.load(root, required=True), 'confirmed',
                                  cleanup={'at': utcnow(), 'generations': report['generations'],
                                           'programs': report['programs'],
                                           'skipped': report['skipped'], 'failed': report['failed'],
                                           'complete': complete})
    return {'ok': complete, 'phase': updated['phase'], 'cleanup': report,
            'complete': complete}
