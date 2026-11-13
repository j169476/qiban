"""The maintenance verbs of one local upgrade transaction, in the order they may run.

    prepare          (installed program; application stopped)
                     copy the maintenance carrier and prove it file by file; write the current
                     generation's preservation and prove it; record the origin program, the real
                     old selection, the target program identity and the carrier; register both
                     generations with their own proofs; then let the existing restore path clone
                     a new generation and switch the pointer to it. A retry is only continued for
                     the target identity the open record already names (`upgrade_target_mismatch`
                     otherwise); a half-finished attempt of that same target is dropped the way
                     `abort` drops it, while a transaction that is already going back is refused
                     instead of dropped.
    confirm          (new program, after its backend reports ready)
                     the program recorded as the target is the one running and its whole
                     published payload re-hashes; the carrier still does; the pointer names the
                     clone; the clone's schema set and manifest decrypt. Only then is the
                     pairing confirmed, the clone's own preservation registered, and older
                     artifacts cleaned.
    rollback-prepare (new program; application stopped; host verification installed)
                     the only code that can read the new schema preserves the current data and
                     seals this machine's local whitelist with the *fallback* generation's own
                     key. Nothing is written in clear text and the previous program never has to
                     read the new schema.
    rollback         (previous program, executed from the carrier; host verification installed)
                     opens the recorded fallback generation with the previous program's own
                     profile, applies the sealed whitelist and the existing recovery rules,
                     switches the pointer, then writes the previous published file set over the
                     installed directory and removes the newer build's extra published files -
                     both from the two manifests persisted in the record, so an interrupted
                     attempt is finished from the record instead of from a mixed directory.
    emergency-seal   (previous program, executed from the carrier; host verification installed)
                     the new version's own backend cannot run, so no preservation can be produced
                     by it. The whole installation is copied offline into
                     ``<root>/upgrade/emergency/<transaction>`` with the credential slots it needs
                     sealed by the per-user DPAPI call, proved by a digest manifest and pinned;
                     the pointer, the program directory and the record stay as they are.
    emergency-rollback
                     (the same carrier; host verification installed) the recorded fallback is
                     opened with the previous program's own schema, the recovery rules hold every
                     platform, the pointer moves back and the previous published set is written.
                     The faulty new version's own local settings are not carried back and no
                     answer claims they were.
    cleanup          repeatable cleanup of the artifacts this installation's upgrades registered.
    abort            a prepared attempt is dropped, or an interrupted staging attempt is cleaned
                     (its database and directory) so the upgrade can be prepared again.
    status           read-only summary; it never writes the record.

`prepare` and `confirm` are wired through the frozen worker's maintenance entry. The rollback
verbs - the ordinary pair and the emergency pair - are **not**: they refuse unless a native host
installs its Windows-verification channel (``verification.install``), which this stage does not
do, so no user-reachable path can roll back without that verification.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from credential_vault import VaultCredentials

from companion_core.errors import AssistantError
from companion_core.postgres import verify_schema
from companion_core.storage import utcnow
from qiban.desktop_profile import desktop_profile

from . import cleanup, data, emergency, journal, locks, program, registry, verification

RUNNING_MARKER = 'Local\\QibanDesktopApplicationRunning'


def _refuse(code, message):
    raise AssistantError(code, message)


def _identity(value, field):
    if not isinstance(value, str) or not journal.DIGEST.fullmatch(value):
        _refuse('upgrade_state', f'{field}无效。')
    return value


def executing_app():
    """The program directory whose code is running, taken from the process, never an argument."""
    path = Path(sys.executable)
    if path.parent.name == 'storage' and path.parent.parent.name == 'resources':
        return path.parent.parent.parent
    return path.parent


def running():
    """Whether a Qiban GUI process holds its marker; an existence check, never a wait."""
    try:
        import ctypes
        from ctypes import wintypes as w

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        open_mutex = kernel.OpenMutexW
        open_mutex.restype, open_mutex.argtypes = w.HANDLE, (w.DWORD, w.BOOL, w.LPCWSTR)
        close = kernel.CloseHandle
        close.restype, close.argtypes = w.BOOL, (w.HANDLE,)
        handle = open_mutex(0x00100000, False, RUNNING_MARKER)
        if not handle:
            return False
        close(handle)
        return True
    except (ImportError, OSError, AttributeError):
        return False


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


def _resume(root, record):
    """Advance a `staging` record whose pointer already moved; the id is derived, not scanned."""
    if record is None or record['phase'] != 'staging' or record['plan'] is None:
        return record
    if data.selection_generation(root, record['installation']) != record['plan']['generation']:
        return record
    descriptor = data.selected(root, record['installation'])
    clone = {**record['plan'], 'manifest_sha256': descriptor['manifest_sha256']}
    return journal.advance(root, record, 'staged', clone=clone)


def status(root, app=None):
    """Read-only: the record, the real selection, the registry and the rollback availability."""
    root = _root(root)
    state_value = data.state(root)
    record = journal.load(root)
    current = journal.selection(data.selected(root, state_value['installation']))
    report = {'ok': True, 'installation': state_value['installation'], 'selection': current,
              'phase': record['phase'] if record else None,
              'transaction': record['id'] if record else None,
              'reconcile': journal.reconcile_hint(record, current['generation']),
              'rollback_available': verification.installed(),
              'emergency': _sealed_ids(root, state_value['installation'], record),
              'registry': registry.load(root, state_value['installation'])}
    if record is not None:
        report.update({'target': record['target']['identity_sha256'],
                       'carrier': record['carrier']['identity_sha256'],
                       'clone': record['clone']['generation'] if record['clone'] else None,
                       'cleanup': record['cleanup']})
    if app is not None:
        try:
            report['program'] = program.document(_app(app))['sha256']
        except AssistantError as error:
            report['program_code'] = error.code
    return report


def _sealed_ids(root, installation_id, record):
    """Read-only: which generations and program copies a completed emergency archive still needs.

    A reader must not fail because the index is unreadable, so the refusal is reported as a field
    instead of being raised here; the verbs that write keep refusing.
    """
    try:
        generations, programs = emergency.pinned(root, installation_id)
    except AssistantError as error:
        return {'ok': False, 'error': error.code, 'reason': str(error)}
    current = None
    if record is not None and record['emergency'] is not None:
        current = {'transaction': record['emergency']['archive']['transaction'],
                   'files': record['emergency']['archive']['count']}
    return {'ok': True, 'generations': sorted(generations, key=lambda item: item or ''),
            'programs': sorted(programs), 'transaction': current}


def prepare(root, app, *, target_identity, vault=None, checkpoint=None, before_commit=None):
    """Pair the installed program with a freshly cloned generation; nothing else is written.

    An unfinished transaction is continued only for the target program it was prepared for, and
    only for that one: a different package is refused with ``upgrade_target_mismatch`` and nothing
    is advanced, cloned or switched. Three cases of the same target are told apart:

    * ``staged`` - or ``staging`` whose pointer already moved - is continued, and the original
      transaction and clone are returned;
    * ``prepared`` or ``staging`` whose pointer never moved is a half-finished attempt nobody can
      finish, so it is dropped through the steps ``abort`` uses (the clone's database, its
      directory and its allocation entry) and this call starts a new transaction instead;
    * a transaction that is already on its way back (``rollback-ready``, ``rolling-back``, an
      emergency branch) is refused, never dropped: finishing or abandoning it is a different verb,
      not a side effect of preparing again.

    Nothing of a half-finished attempt is abandoned unless the recorded origin is still the
    selected generation, so a pointer that moved somewhere else is refused instead of cleaned.
    """
    root, app = _root(root), _app(app)
    target = _identity(target_identity, '目标程序身份')
    vault = vault if vault is not None else VaultCredentials()
    with locks.exclusive(root):
        if running():
            _refuse('upgrade_running', '栖伴仍在运行，请先在窗口中正常退出后再继续。')
        record = journal.load(root)
        if record is not None and record['phase'] in journal.OPEN_PHASES:
            if record['target']['identity_sha256'] != target:
                _refuse('upgrade_target_mismatch',
                        '上一次没有结束的升级事务是为另一个新版本准备的，未继续那次事务，'
                        '也没有改动它的进度、克隆或指针。请先完成或放弃那次升级，'
                        '或者用记录里的那个新版本重试。')
            report, abandoned = _continue(root, record, app=app)
            if report is not None:
                return report
            report = _start(root, app, target, vault=vault, checkpoint=checkpoint,
                            before_commit=before_commit)
            if abandoned is not None:
                report['abandoned'] = abandoned
            return report
        return _start(root, app, target, vault=vault, checkpoint=checkpoint,
                      before_commit=before_commit)


def _continued(record):
    """The answer for a transaction this call continues; ``staged`` is the only such phase."""
    if record['phase'] != 'staged':
        _refuse('upgrade_pending',
                '上一次升级事务还没有结束，也不能从准备流程继续；请先完成或人工处理那次事务。')
    return {'ok': True, 'phase': record['phase'], 'transaction': record['id'],
            'generation': record['clone']['generation'], 'resumed': True,
            'carrier': record['carrier']['identity_sha256'],
            'preservation': record['preservation']['name']}


def _origin(root, record, selected):
    """Only the recorded origin may authorize abandoning a half-finished attempt.

    A pointer that already names something else is refused instead of cleaned: whatever is
    selected now is not part of this transaction, and neither its clone nor anybody else's
    generation may be dropped on the strength of an interrupted record.
    """
    if selected != record['origin']['selection']['generation']:
        _refuse('upgrade_state',
                '当前选择的代次不是这次升级开始时的代次；未清理半成品，也没有放弃这次事务，'
                '请人工检查。')


def _continue(root, record, *, app):
    """Continue one open transaction for the same target, or drop a half-finished attempt.

    Returns ``(report, abandoned)``: a report when this call continues the transaction, or
    ``None`` with what was removed when the caller may start a new one.

    The drop happens inside the maintenance section the caller already holds - through
    ``_drop_incomplete`` and the ``aborted`` step, never through the public ``abort`` verb, which
    takes those locks again. It does not take the portable restore lock the ``abort`` verb also
    holds, because it cannot be held here: the clone of this very verb calls
    ``portable_restore.stage_restore``, which takes ``restore.lock`` itself, so a section-wide
    acquisition would deadlock the clone. The restore path is already excluded by the lock this
    section does hold - ``installation.lock`` is acquired for the whole run of the business worker
    that is the only caller of ``stage_restore``
    (``storage_worker.py`` main's guard, released in its final ``finally``), and this section
    acquires the same file, so neither can be in flight while the other is - and this cleanup
    touches only the clone of the record being abandoned.
    """
    if record['phase'] == 'staged':
        return _continued(record), None
    plan = record['plan']
    if record['phase'] == 'staging' and plan is not None:
        selected = data.selection_generation(root, record['installation'])
        if selected == plan['generation']:
            return _continued(_resume(root, record)), None
        _origin(root, record, selected)
        abandoned = _drop_incomplete(root, record, plan, app=app)
        journal.advance(root, journal.load(root, required=True), 'aborted')
        return None, abandoned
    if record['phase'] == 'prepared':
        _origin(root, record, data.selection_generation(root, record['installation']))
        journal.advance(root, journal.load(root, required=True), 'aborted')
        return None, None
    _refuse('upgrade_pending',
            '上一次升级事务已经进入回退或封存阶段，不能用准备流程继续；'
            '请先完成或人工处理那次事务。')


def _start(root, app, target, *, vault, checkpoint, before_commit):
    """A fresh transaction for one installed program; the caller holds the maintenance locks."""
    state_value = data.state(root)
    installation_id = state_value['installation']
    published = program.document(app)
    _identity(published['sha256'], '当前程序身份')
    if published['sha256'] == target:
        _refuse('upgrade_state', '目标程序与当前程序相同，不需要升级。')
    transaction = os.urandom(16).hex()
    carrier = program.copy_carrier(root, app, published, installation=installation_id,
                                   transaction=transaction, roles=['carrier'])
    carried = program.carrier_record(carrier)
    with data.session(root, app, vault=vault) as (store, key, _, _, descriptor):
        preservation = data.preserve(store, key)
        fingerprint = data.fingerprint(store)
        migrations = data.migrations(store)
        selection = journal.selection(descriptor)
        artifact = {'name': preservation['name'], 'sha256': preservation['sha256'],
                    'bytes': preservation['bytes'], 'generation': descriptor['generation'],
                    'store': selection['store']}
        origin = {'identity_sha256': published['sha256'], 'selection': selection,
                  'data': {'fingerprint': fingerprint, 'migrations_sha256': migrations}}
        record = journal.create(installation_id, origin, {'identity_sha256': target},
                                {'identity_sha256': carried['identity_sha256'],
                                 'count': carried['count'], 'bytes': carried['bytes']},
                                artifact, transaction=transaction)
        registry.register_program(root, installation_id, published, transaction, 'carrier')
        registry.register_generation(root, installation_id, selection, transaction,
                                     manifest_sha256=selection['manifest_sha256'],
                                     preservation=artifact,
                                     source_fingerprint=fingerprint)
        record = journal.save(root, record)
        plan = data.plan_clone(installation_id, transaction)
        registry.register_generation(root, installation_id, plan, transaction)
        record = journal.advance(root, record, 'staging', plan=plan)
        blob, source_key = data.preserved_pair(root, installation_id, vault, artifact)
        seen = {}

        def commit_hook(facts):
            if facts['generation'] != plan['generation']:
                _refuse('upgrade_state', '克隆代次与升级记录不一致，未继续维护操作。')
            seen.update(facts)
            if before_commit is not None:
                before_commit(facts)

        data.clone(root, store, installation_id=installation_id, transaction=transaction,
                   preservation=artifact, blob=blob, source_key=source_key,
                   expected_fingerprint=fingerprint,
                   application_session='upgrade-'+transaction, before_commit=commit_hook,
                   vault=vault, checkpoint=checkpoint)
    if 'manifest_sha256' not in seen:
        _refuse('upgrade_state', '克隆提交前的核对没有执行，未继续维护操作。')
    clone = {**plan, 'manifest_sha256': seen['manifest_sha256']}
    record = journal.advance(root, journal.load(root, required=True), 'staged', clone=clone)
    return {'ok': True, 'phase': record['phase'], 'transaction': record['id'],
            'generation': clone['generation'], 'carrier': record['carrier']['identity_sha256'],
            'preservation': record['preservation']['name']}


def confirm(root, app, *, vault=None):
    """Only the recorded target program, only after it reads the clone, may confirm."""
    root, app = _root(root), _app(app)
    vault = vault if vault is not None else VaultCredentials()
    state_value = data.state(root)
    installation_id = state_value['installation']
    published = program.document(app)
    record = journal.load(root, required=True)
    if record['phase'] == 'confirmed':
        if record['confirmed']['identity_sha256'] != published['sha256']:
            _refuse('upgrade_state', '这次升级已经由另一个程序确认过。')
        return {'ok': True, 'phase': 'confirmed', 'already': True, 'transaction': record['id']}
    if record['phase'] != 'staged':
        _refuse('upgrade_state', '这次升级不是待确认状态，未做任何改动。')
    if published['sha256'] != record['target']['identity_sha256']:
        _refuse('upgrade_confirm_program', '当前程序不是这次升级记录里的目标程序，未确认本次升级。')
    program.verify(app, published)
    carrier = program.tree(root, record['carrier']['identity_sha256'])
    previous = program.document(carrier)
    if previous['sha256'] != record['carrier']['identity_sha256']:
        _refuse('upgrade_program_changed', '维护载体与升级记录不符，未确认本次升级。')
    program.verify(carrier, previous)
    with locks.exclusive(root):
        current = journal.load(root, required=True)
        if current['phase'] != 'staged' or current['id'] != record['id']:
            _refuse('upgrade_state', '升级事务记录在确认期间已变化。')
        if data.selection_generation(root, installation_id) != current['clone']['generation']:
            _refuse('upgrade_state', '当前资料代次不是这次升级的新代次，未确认。')
        with data.live(root, vault=vault) as (store, key, _, _, descriptor):
            verify_schema(store.database)
            store.read('manifest')
            if descriptor['generation'] != current['clone']['generation']:
                _refuse('upgrade_state', '正在读取的资料代次不是这次升级的新代次，未确认。')
            fingerprint = data.fingerprint(store)
            migrations = data.migrations(store)
            preservation = data.preserve(store, key)
        artifact = {'name': preservation['name'], 'sha256': preservation['sha256'],
                    'bytes': preservation['bytes'], 'generation': current['clone']['generation'],
                    'store': current['clone']['store']}
        data.verify_artifact(root, artifact, key=key)
        record = journal.advance(root, current, 'confirmed',
                                 confirmed={'identity_sha256': published['sha256'],
                                            'at': utcnow(),
                                            'data': {'fingerprint': fingerprint,
                                                     'migrations_sha256': migrations}})
        registry.register_generation(root, installation_id, record['clone'], record['id'],
                                     manifest_sha256=record['clone']['manifest_sha256'],
                                     preservation=artifact, source_fingerprint=fingerprint)
        registry.register_program(root, installation_id, published, record['id'], 'previous')
    cleaned = None
    try:
        cleaned = cleanup.clean(root, state_value=state_value, vault=vault)
    except AssistantError as error:
        cleaned = {'ok': False, 'error': error.code, 'reason': str(error)}
    return {'ok': True, 'phase': 'confirmed', 'transaction': record['id'],
            'identity_sha256': published['sha256'], 'cleanup': cleaned}


def rollback_prepare(root, *, app=None, vault=None):
    """The new program preserves the current data and seals the local whitelist for the way back."""
    root = _root(root)
    app = _app(app if app is not None else executing_app())
    vault = vault if vault is not None else VaultCredentials()
    record = journal.load(root, required=True)
    if record['phase'] == 'rollback-ready':
        return {'ok': True, 'phase': 'rollback-ready', 'already': True,
                'transaction': record['id']}
    if record['phase'] not in ('staged', 'confirmed'):
        _refuse('upgrade_state', '这次升级不是可回退状态，未做任何改动。')
    published = program.document(app)
    if published['sha256'] != record['target']['identity_sha256']:
        _refuse('upgrade_rollback_program',
                '只有这次升级装入的新版本才能保全当前资料；请从该版本执行回退准备。')
    receipt = verification.authorize(record)
    installation_id = record['installation']
    with locks.exclusive(root, restore=True):
        if running():
            _refuse('upgrade_running', '栖伴仍在运行，请先在窗口中正常退出后再继续。')
        current = journal.load(root, required=True)
        if current['phase'] not in ('staged', 'confirmed'):
            _refuse('upgrade_state', '升级事务记录在准备回退期间已变化。')
        with data.session(root, app, vault=vault, profile=desktop_profile()) as (
                store, key, _, _, descriptor):
            if descriptor['generation'] != current['clone']['generation']:
                _refuse('upgrade_state', '当前资料代次与升级记录不一致，未继续回退。')
            preservation = data.preserve(store, key)
            fingerprint = data.fingerprint(store)
            migrations = data.migrations(store)
            settings = data.local_settings(store, installation_id)
            selection = journal.selection(descriptor)
        fallback = current['origin']['selection']
        fallback_key = data.slot_key(vault, installation_id, fallback['credential_id'])
        sealed = data.seal_settings(settings, fallback_key, keyref=fallback['credential_id'])
        artifact = {'name': preservation['name'], 'sha256': preservation['sha256'],
                    'bytes': preservation['bytes'], 'generation': descriptor['generation'],
                    'store': selection['store']}
        record = journal.advance(root, journal.load(root, required=True), 'rollback-ready',
                                 rollback={'at': utcnow(), 'receipt': receipt,
                                           'preservation': artifact, 'preserved_locally': False,
                                           'settings': sealed, 'progress': None})
    return {'ok': True, 'phase': 'rollback-ready', 'transaction': record['id'],
            'preservation': artifact['name'],
            'current': {'fingerprint': fingerprint, 'migrations_sha256': migrations}}


def rollback(root, *, installed_app, vault=None):
    """Switch back from the carrier: previous schema only, sealed whitelist, persisted plan."""
    root = _root(root)
    installed_app = _app(installed_app)
    library = _app(executing_app())
    if library == installed_app:
        _refuse('upgrade_executing_program',
                '回退必须由维护载体执行，不能在正在被覆盖的程序目录里运行；未做任何改动。')
    vault = vault if vault is not None else VaultCredentials()
    record = journal.load(root, required=True)
    if record['phase'] == 'rolled-back':
        return {'ok': True, 'phase': 'rolled-back', 'already': True, 'transaction': record['id']}
    if record['phase'] not in ('rollback-ready', 'rolling-back'):
        _refuse('upgrade_state', '这次升级不是可回退状态，未做任何改动。')
    installation_id = record['installation']
    previous = program.document(library)
    if previous['sha256'] != record['carrier']['identity_sha256']:
        _refuse('upgrade_executing_program', '当前执行程序不是这次升级记录的维护载体，未回退。')
    program.verify(library, previous)
    progress = record['rollback']['progress']
    allowed = {record['target']['identity_sha256']}
    if progress is not None:
        # A previous attempt may already have written the previous identity document: the retry
        # finishes from the persisted plan instead of demanding that the directory still looks
        # like the newer build, which would otherwise deadlock a half-written rollback.
        allowed.add(progress['previous_identity'])
    try:
        installed = program.document(installed_app)
    except AssistantError:
        installed = None
    if progress is None and (installed is None or installed['sha256'] not in allowed):
        _refuse('upgrade_program_changed', '当前程序目录不是这次升级装入的新版本，未回退。')
    verification.authorize(record)
    with locks.exclusive(root, restore=True):
        if running():
            _refuse('upgrade_running', '栖伴仍在运行，请先在窗口中正常退出后再继续。')
        record = journal.load(root, required=True)
        if record['phase'] == 'rollback-ready':
            progress = {'at': utcnow(),
                        'pointer_before': data.selection_generation(root, installation_id),
                        'pointer_switched': False,
                        'previous_files': previous['files'], 'target_files': installed['files'],
                        'previous_identity': previous['sha256'],
                        'target_identity': installed['sha256']}
            record = journal.advance(root, record, 'rolling-back',
                                     rollback={**record['rollback'], 'progress': progress})
        progress = record['rollback']['progress']
        if progress['previous_identity'] != record['carrier']['identity_sha256']:
            _refuse('upgrade_state', '回退进度与维护载体不符，未继续回退。')
        if not progress['pointer_switched']:
            _switch_back(root, record, library, vault)
        plan = {'write': progress['previous_files'],
                'remove': sorted({row['path'] for row in progress['target_files']}
                                 - {row['path'] for row in progress['previous_files']})}
        program.apply_plan(plan, library, installed_app)
        program.verify_plan(plan, installed_app, progress['previous_identity'])
        record = journal.advance(root, journal.load(root, required=True), 'rolled-back')
    return {'ok': True, 'phase': 'rolled-back', 'transaction': record['id'],
            'generation': record['origin']['selection']['generation'],
            'preservation': record['rollback']['preservation']['name'],
            'removed': plan['remove']}


def _switch_back(root, record, library, vault):
    """Apply the sealed whitelist and the restore rules to the fallback, then move the pointer."""
    installation_id = record['installation']
    fallback = record['origin']['selection']
    existing = data.selection_generation(root, installation_id)
    if existing == fallback['generation']:
        # Pointer commit succeeded before the progress write. Recovery settings were applied
        # before that commit, so do not apply them twice (lock revisions/tasks are not no-ops).
        actual = data.selected(root, installation_id)
        if any(actual[field] != fallback[field] for field in (
                'generation', 'database', 'credential_id', 'manifest_sha256')):
            _refuse('upgrade_state', '已切回的资料与记录中的回退目标不一致。')
        journal.advance(root, record, 'rolling-back',
                        rollback={**record['rollback'],
                                  'progress': {**record['rollback']['progress'],
                                               'at': utcnow(), 'pointer_switched': True}})
        return
    if existing != record['rollback']['progress']['pointer_before']:
        _refuse('upgrade_state', '当前代次已不是回退开始时的代次，未回退。')
    key = data.slot_key(vault, installation_id, fallback['credential_id'])
    settings = data.unseal_settings(record['rollback']['settings'], key)
    with data.session(root, library, vault=vault, profile=desktop_profile(),
                      selection=fallback) as (store, _, _, _, descriptor):
        if descriptor['generation'] != fallback['generation']:
            _refuse('upgrade_state', '回退目标代次与升级记录不符，未回退。')
        with store.db() as db:
            data.apply_recovery(store, settings, application_session='upgrade-'+record['id'],
                                generation=fallback['generation'], db=db)
    current = data.selected(root, installation_id)
    data.commit_selection(root, installation_id, fallback['generation'], current)
    journal.advance(root, journal.load(root, required=True), 'rolling-back',
                    rollback={**record['rollback'],
                              'progress': {**record['rollback']['progress'], 'at': utcnow(),
                                           'pointer_switched': True}})


def emergency_seal(root, *, app=None, vault=None):
    """Seal the faulty new version's material offline; only the previous program may call this.

    The new version's backend cannot run, so no preservation copy and no sealed whitelist can be
    written by it. This verb does the one thing that is possible without it: copy the whole
    installation out of the stopped root, seal the credential slots the copy needs and pin both,
    all before anything is switched. The program directory comes from the process, never from an
    argument, and it must be this transaction's recorded maintenance carrier.
    """
    root = _root(root)
    library = _app(app if app is not None else executing_app())
    return emergency.seal(root, library=library,
                          vault=vault if vault is not None else VaultCredentials())


def emergency_rollback(root, *, installed_app, app=None, vault=None):
    """Bring the previous build back from the carrier, after the seal; nothing new is preserved."""
    root = _root(root)
    installed_app = _app(installed_app)
    library = _app(app if app is not None else executing_app())
    if library == installed_app:
        _refuse('upgrade_executing_program',
                '紧急回退必须由维护载体执行，不能在正在被覆盖的程序目录里运行；未做任何改动。')
    return emergency.switch_back(root, installed_app=installed_app, library=library,
                                 vault=vault if vault is not None else VaultCredentials())


def abort(root, *, app=None):
    """Drop a prepared attempt, or clean an interrupted staging attempt so it can be retried."""
    root = _root(root)
    record = journal.load(root, required=True)
    if record['phase'] == 'prepared':
        with locks.exclusive(root, restore=True):
            record = journal.advance(root, journal.load(root, required=True), 'aborted')
        return {'ok': True, 'phase': record['phase'], 'transaction': record['id'],
                'cleaned': None}
    if record['phase'] != 'staging':
        _refuse('upgrade_state', '只有准备阶段或克隆阶段的升级可以放弃；已切换的请确认或回退。')
    plan = record['plan']
    if plan is None:
        _refuse('upgrade_state', '克隆阶段的升级记录缺少克隆计划，未做任何改动。')
    with locks.exclusive(root, restore=True):
        record = journal.load(root, required=True)
        if record['phase'] != 'staging' or record['plan'] is None:
            _refuse('upgrade_state', '升级事务记录在放弃期间已变化。')
        if data.selection_generation(root, record['installation']) == plan['generation']:
            _refuse('upgrade_state', '这个克隆已经切换为当前代次，不能放弃，请确认或回退。')
        cleaned = _drop_incomplete(root, record, plan, app=app)
        record = journal.advance(root, journal.load(root, required=True), 'aborted')
    return {'ok': True, 'phase': record['phase'], 'transaction': record['id'], 'cleaned': cleaned}


def _drop_incomplete(root, record, plan, *, app):
    """Remove the database and directory of a clone that never became the selection."""
    installation_id = record['installation']
    directory = Path(root)/'generations'/plan['generation']
    dropped = None
    if directory.exists() or not data.free_port(data.state(root)):
        with data.admin(root, app=app) as admin:
            dropped = data.drop_database(admin, plan['database'])
    removed = False
    if directory.exists():
        if not directory.is_dir() or directory.is_symlink():
            _refuse('upgrade_state', '半成品目录不是普通目录，未清理。')
        shutil.rmtree(directory, ignore_errors=True)
        removed = not directory.exists()
        if not removed:
            _refuse('upgrade_state', '半成品代次目录未能完全删除，保留登记以便重试。')
    registry.drop_generation(root, installation_id, plan['generation'])
    return {'generation': plan['generation'], 'database': plan['database'],
            'dropped': dropped, 'directory_removed': removed}


def core_cleanup(root, *, vault=None, app=None):
    """Repeatable cleanup of this installation's registered upgrade artifacts."""
    root = _root(root)
    vault = vault if vault is not None else VaultCredentials()
    return cleanup.clean(root, vault=vault, app=app)
