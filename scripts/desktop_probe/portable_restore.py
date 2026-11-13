"""Trusted host staging of a portable (cross-machine) restore into an isolated generation.

A portable restore never rewrites the current database in place. It stages one new
generation inside the *current* installation: a new database in the same PostgreSQL
cluster, a new store root under ``<root>/generations/<id>/store`` and a new Windows
credential slot holding the source data key. The current database, the current store and
its assets, the current credential slot and every earlier generation are only read, so an
interruption before the commit point leaves the current selection exactly as it was.

``stage_restore()`` is the only entry point and trusted host code calls it (the native
stager, after the Windows verification in ``desktop_portable.complete()``). It is not a
management operation, not a model tool, and it starts no model and no platform. The commit
point is the single ``<root>/active-generation.json`` replacement owned by
``installation_generation.commit_selection``; this module returns only after that
replacement, together with ``restart_required``, and the restart then reads the new
generation.

Order of work, each step a durable journal phase in
``<root>/generations/<id>/restore-journal.json``. The journal holds only non-secret ids,
phases, digests and the basenames of the source the host named and of this machine's own
preservation copy, which are never merged into one name:

    created      the generation directory and store root exist, owned by this Windows user
    database     the cluster is this installation's and qiban_restore_<id> was created
    schema       the new store was migrated with desktop_profile and initialized
    restored     the snapshot replaced the staged store and its assets landed in it
    validated    every encrypted payload, asset and the schema of the staged store re-read
    prepared     the ready manifest was registered; the commit point was not touched
    committed    the commit point now names this generation

Nothing is deleted on failure: a staged database and directory stay as evidence, and the
journal records the phase with a fixed error code. Chinese messages never contain a
password, a data key, a payload or a digest of one.

Refusals are AssistantError with a stable code: portable_root, portable_input,
portable_busy, portable_lock_failed, portable_changed, portable_preserve, portable_schema,
portable_current, portable_directory_permissions, portable_stage_exists, portable_database,
portable_roles, portable_vault, portable_source_identity, portable_validate and
portable_switch_applied. ``installation_generation`` codes (generation_path,
generation_store_missing, generation_busy, generation_selection_stale...) and the snapshot
validators' own codes pass through unchanged: they already say what the trusted root or the
source rejected.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace

import installation_generation
import psycopg
from credential_vault import VaultCredentials
from filelock import FileLock, Timeout
from install_windows import protect_directory
from installation import VAULT, authorize_runtime, read
from psycopg import sql

from companion_core import asset_transfer, portable_validation, postgres_transfer
from companion_core.contracts import ActorRef, stable_key
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.postgres import PostgresStore, verify_schema
from companion_core.runtime_keys import DESKTOP_PLATFORMS
from companion_core.storage import atomic_write, utcnow
from companion_core.tasks import ExplicitTasks
from qiban import desktop_identity, desktop_wechat
from qiban.desktop_management import LOCK
from qiban.desktop_manual_tasks import HANDOFF, QUEUE
from qiban.desktop_portable import PRESERVATION
from qiban.desktop_runtime import HOLD, PREFERENCES, SESSION

JOURNAL = 'restore-journal.json'
LOCK_FILE = 'restore.lock'
# Where the portable preview keeps its preservation copy; the same constant the service uses.
LOCAL_STATE_KEYS = postgres_transfer.LOCAL_STATE_KEYS
MAX_SESSION = 120


def _root(root):
    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AssistantError('portable_root', '本机资料目录不可用，未切换资料。') from None
    if not resolved.is_dir() or not (resolved/'installation.json').is_file():
        raise AssistantError('portable_root', '本机资料目录不可用，未切换资料。')
    return resolved


def _installation(root):
    """The journal of the current installation; only a ready installation stages a restore."""
    try:
        state = read(root/'installation.json')
    except (OSError, ValueError):
        raise AssistantError('portable_root', '安装记录无法读取，未切换资料。') from None
    identifier = state.get('installation') if isinstance(state, dict) else None
    if (not isinstance(state, dict) or state.get('format') != 1
            or not isinstance(identifier, str)
            or not installation_generation.IDENTIFIER.fullmatch(identifier)
            or type(state.get('port')) is not int or not 1024 <= state['port'] <= 65535
            or state.get('phase') != 'ready'):
        raise AssistantError('portable_root', '安装记录不是已就绪的安装，未切换资料。')
    return state


def _blob(source_blob):
    if not isinstance(source_blob, bytes) or not 0 < len(source_blob) <= postgres_transfer.MAX_BACKUP:
        raise AssistantError('portable_input', '备份内容无效或超过上限。')
    return source_blob


def _source_key(source_key):
    if not isinstance(source_key, bytes) or len(source_key) != 32:
        raise AssistantError('portable_input', '恢复密钥无效。')
    return source_key


def _fingerprint(value):
    if not isinstance(value, str) or not installation_generation.DIGEST.fullmatch(value):
        raise AssistantError('portable_input', '当前资料指纹无效。')
    return value


def _session(value):
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SESSION:
        raise AssistantError('portable_input', '应用会话标识无效。')
    return value


def _preservation(value):
    if not isinstance(value, dict) or set(value) != {'name', 'sha256', 'bytes'}:
        raise AssistantError('portable_preserve', '恢复前保全备份记录无效，未切换资料。')
    name = value['name']
    digest = value['sha256']
    if (not isinstance(name, str) or not name or '/' in name or '\\' in name or ':' in name
            or not name.endswith('.ccbackup') or not isinstance(digest, str)
            or not installation_generation.DIGEST.fullmatch(digest)
            or type(value['bytes']) is not int or not 0 < value['bytes'] <= postgres_transfer.MAX_BACKUP):
        raise AssistantError('portable_preserve', '恢复前保全备份记录无效，未切换资料。')
    return {'name': name, 'sha256': digest, 'bytes': value['bytes']}


def _preserved(store, preservation):
    """The preservation artifact must still be the authenticated copy the preview wrote."""
    folder = Path(store.root)/'history'/PRESERVATION
    path = folder/preservation['name']
    try:
        entry = path.lstat()
    except OSError:
        raise AssistantError('portable_preserve', '恢复前保全备份不存在，未切换资料。') from None
    if (not path.is_file() or path.is_symlink() or path.resolve().parent != folder.resolve()
            or entry.st_size != preservation['bytes']):
        raise AssistantError('portable_preserve', '恢复前保全备份已改变，未切换资料。')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != preservation['sha256']:
        raise AssistantError('portable_preserve', '恢复前保全备份校验失败，未切换资料。')
    postgres_transfer.unpack(raw, store._backup_key)
    return preservation


def _backup_key(data_key):
    """Same derivation as ``PostgresStore._backup_key`` and the portable export."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b'companion-postgres-backup-v1').derive(data_key)


def _journal(path, phase, value):
    """One durable journal file; it never holds a secret, a key or payload text."""
    entry = {'format': 1, 'phase': phase, 'at': utcnow(), **value}
    atomic_write(path, json.dumps(entry, ensure_ascii=False, sort_keys=True).encode('utf8'))
    return entry


def _best_effort(path, phase, value, error):
    """Evidence is best effort; it must never replace the failure being reported."""
    try:
        _journal(path, phase, {**value, 'error': error})
    except (OSError, AssistantError):
        pass


def _source_backup(source_name):
    """The trusted basename of the data that is being restored, or None when unnamed.

    Only a trusted host names the source and the value is display evidence, never a path;
    the basename rule is the one the journal already used. The stager may be called with the
    authenticated pair alone (the desktop service hands no display name), so a source the
    host did not name stays unnamed: the preservation copy written just before the switch
    holds this machine's own data and never stands in for the source.
    """
    if not isinstance(source_name, str) or not source_name:
        return None
    return Path(source_name).name[:120] or None


def _stamp(state, source_name, blob, preservation, expected, generation, source_mode='portable'):
    """Non-secret identifiers of one staging attempt, kept in the journal."""
    return {'installation': state['installation'], 'generation': generation, 'source_mode': source_mode,
            'source_backup': _source_backup(source_name),
            'source_sha256': hashlib.sha256(blob).hexdigest(), 'source_bytes': len(blob),
            'preservation': dict(preservation), 'expected_current': expected}


def _local_settings(store, installation):
    """This machine's own records, keyed for this installation; the source decides none of them.

    Only an absent value from this machine is recorded as absent, so the caller removes the
    source's row for that key instead of inheriting it. `desktop_wechat.selection` and
    `desktop_wechat._profile` are the real settings code, so a legacy enrollment without a
    selection record is enumerated the same way the application reads it.
    """
    owner = store.state(desktop_identity.OWNER)
    if (not isinstance(owner, dict) or owner.get('installation') != installation
            or not isinstance(owner.get('actor'), dict)):
        raise AssistantError('portable_current', '当前资料不属于本次安装，未切换资料。')
    lock = store.state(LOCK)
    if (not isinstance(lock, dict) or type(lock.get('enabled')) is not bool
            or type(lock.get('revision')) is not int):
        raise AssistantError('portable_current', '当前应用锁状态无效，未切换资料。')
    keys = list(_local_keys(store, installation))
    values = {key: store.state(key) for key in keys}
    values[desktop_identity.OWNER] = owner
    values[LOCK] = {**lock, 'revision': lock['revision']+1}
    return values


def _local_keys(store, installation):
    """Every state key whose value belongs to this machine rather than to a source generation."""
    keys = [desktop_wechat.SETUP, PREFERENCES, stable_key('desktop-wechat-selection', installation),
            *sorted(LOCAL_STATE_KEYS)]
    selected = desktop_wechat.selection(store, installation)
    wanted = {selected.get('selected'), *(selected.get('accounts') or [])}
    for login in sorted(value for value in wanted if isinstance(value, str) and value):
        scope, setup_key = desktop_wechat._profile(store, installation, login)
        keys += [setup_key, desktop_wechat.enrollment_key(scope)]
    return keys


def _apply_local(store, settings, db):
    """Write this machine's values; a key this machine does not hold is deleted, not inherited."""
    for key, value in settings.items():
        if value is None:
            db.execute('DELETE FROM state WHERE key=?', (key,))
        else:
            store.set_state(key, value, db)


def _base_credential(vault, installation):
    """Bootstrap and runtime passwords of this cluster live in the installation's own slot."""
    try:
        raw = vault.get_password(VAULT, installation)
    except Exception:
        raise AssistantError('portable_vault', '凭据库不可用，未切换资料。') from None
    if raw is None:
        raise AssistantError('portable_vault', '本次安装的凭据缺失，未切换资料。')
    try:
        value = json.loads(raw)
        bootstrap, password = value['bootstrap_password'], value['database_password']
    except (KeyError, TypeError, ValueError):
        raise AssistantError('portable_vault', '本次安装的凭据无效，未切换资料。') from None
    if (not isinstance(bootstrap, str) or not isinstance(password, str)
            or len(bootstrap) < 32 or len(password) < 32):
        raise AssistantError('portable_vault', '本次安装的凭据无效，未切换资料。')
    return {'bootstrap_password': bootstrap, 'database_password': password}


def _options(state, credential, dbname):
    return {'host': '127.0.0.1', 'port': state['port'], 'user': 'qiban_setup',
            'password': credential['bootstrap_password'], 'dbname': dbname, 'connect_timeout': 5}


def _maintenance(state, credential):
    """One autocommit maintenance connection as qiban_setup; never a runtime credential."""
    try:
        return psycopg.connect(**_options(state, credential, 'postgres'), autocommit=True)
    except psycopg.Error:
        raise AssistantError('portable_database', '本机数据库不可用，未切换资料。') from None


def _cluster(connection, root):
    """The server we are about to change must serve this installation's own data directory."""
    try:
        actual = Path(connection.execute('SHOW data_directory').fetchone()[0]).resolve()
    except (psycopg.Error, TypeError):
        raise AssistantError('portable_database', '无法确认数据库目录，未切换资料。') from None
    if actual != (root/'database').resolve():
        raise AssistantError('portable_database', '数据库目录不属于本次安装，未切换资料。')


def _roles(connection):
    """Staging adds no privilege: the owner cannot log in and the runtime cannot create."""
    try:
        rows = {row[0]: row[1:] for row in connection.execute(
            'SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles '
            "WHERE rolname IN ('qiban_owner','qiban_runtime')")}
    except psycopg.Error:
        raise AssistantError('portable_roles', '无法确认数据库角色，未切换资料。') from None
    owner, runtime = rows.get('qiban_owner'), rows.get('qiban_runtime')
    if owner is None or owner[0] is not False:
        raise AssistantError('portable_roles', '本机数据库角色与安装不符，未切换资料。')
    if runtime is None or runtime[1] or runtime[2] or runtime[3]:
        raise AssistantError('portable_roles', '运行账号权限被放宽，未切换资料。')


def _create_database(connection, database):
    """Create exactly one new database from the autocommit maintenance connection.

    PostgreSQL refuses CREATE DATABASE inside a transaction block, so this runs as one
    autocommit statement on the connection that already proved the data directory.
    """
    try:
        connection.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(
            sql.Identifier(database), sql.Identifier('qiban_owner')))
    except psycopg.Error:
        raise AssistantError('portable_database', '无法建立独立代次数据库，未切换资料。') from None


def _grants(database, state, credential):
    """The installer's own runtime grant set, applied to the staged database."""
    try:
        with psycopg.connect(**_options(state, credential, database), autocommit=True) as admin:
            authorize_runtime(admin)
    except psycopg.Error:
        raise AssistantError('portable_database', '无法授予运行账号访问，未切换资料。') from None


def _source_identity(store, db):
    """The logical identities the restored rows carry, validated before this machine adopts them.

    A source binding is accepted only through `desktop_identity.verify`, which cross-checks the
    source installation, its own physical owner, the installed role and the source project id.
    Without a binding, the source administrator actor and role must be exactly scoped to the
    source installation, checked by `desktop_identity.verify_role`; a parsed ActorRef alone is
    not enough. The raw binding value (or its absence) and the source owner are returned so a
    local preservation can restore that exact state instead of inventing one.
    """
    owner = store.state(desktop_identity.OWNER, None, db)
    installation = owner.get('installation') if isinstance(owner, dict) else None
    actor = owner.get('actor') if isinstance(owner, dict) else None
    if not isinstance(actor, dict) or not isinstance(installation, str) or not (
            installation_generation.IDENTIFIER.fullmatch(installation)):
        raise AssistantError('portable_source_identity', '来源资料缺少本机使用者身份，未切换资料。')
    try:
        source_owner = ActorRef(**actor)
    except (AssistantError, TypeError):
        raise AssistantError('portable_source_identity', '来源资料的使用者身份无效，未切换资料。') from None
    if (source_owner.platform != desktop_identity.OWNER_PLATFORM
            or source_owner.scope != installation):
        raise AssistantError('portable_source_identity', '来源资料的使用者身份无效，未切换资料。')
    try:
        bound = desktop_identity.verify(store, installation=installation, owner=source_owner, db=db)
        if bound is not None:
            return {'installation': installation, 'owner': source_owner, 'room': bound['room'],
                    'role': bound['logical_role'], 'binding': desktop_identity.binding(store, db)}
        role = desktop_identity.verify_role(store, store.state(desktop_identity.ROLE, None, db),
                                            installation=installation, owner=source_owner)
    except AssistantError:
        raise AssistantError('portable_source_identity', '来源资料的身份记录无效，未切换资料。') from None
    return {'installation': installation, 'owner': source_owner, 'room': source_owner,
            'role': role, 'binding': None}


def _hold_session(store, application_session, generation, db):
    """Every platform stays held until the user starts it, across restarts.

    The session record holds this application session only; the durable record is what a
    later application session reads, so a restart cannot turn the restored platforms on.
    """
    held = list(DESKTOP_PLATFORMS)
    store.set_state(SESSION, {'application': application_session, 'instance': secrets.token_hex(16),
                              'held': held}, db)
    store.set_state(HOLD, {'format': 1, 'generation': generation, 'platforms': held,
                           'at': utcnow()}, db)


def _refuse(*_):
    raise AssistantError('portable_restore', '暂存阶段不能执行任务')


def _retire_tasks(store, db):
    """Old task queues and handovers must not run after a restore.

    This is the rule of ``DesktopManualTasks.stop('restore')``: ``ExplicitTasks.stop``
    cancels queued or running requests with that reason (the same state transition and
    revision format), and the queue and handover lists are emptied. Unprocessed room input
    is bound to one run generation by ``DesktopRoom`` admission and every run slot was
    paused by the restore reset, so no accepted input executes without a manual start.
    """
    engine = ExplicitTasks(
        SimpleNamespace(store=store), None, authenticate=_refuse,
        session_active=lambda *_: False, handler=_refuse,
        accepts=lambda data: store.state('explicit-task:'+data['id'], None, db) is not None)
    engine.stop('restore')
    store.set_state(QUEUE, [], db)
    store.set_state(HANDOFF, [], db)


def _rows(block, names):
    """The identity tuples of one snapshot table block, for loss detection only."""
    index = {name: position for position, name in enumerate(block['columns'])}
    return {tuple(row[index[name]] for name in names) for row in block['rows']}


def _verified(store, source_key, source):
    """Re-read the staged generation: schema, encrypted payloads, history, ledger and assets.

    The staged store must hold at least every source record and history version: a service
    repair may legitimately add records or history, but no source history may be lost.
    """
    verify_schema(store.database)
    store.read('manifest')
    if not store.validate_all()['ok']:
        raise AssistantError('portable_validate', '暂存资料未通过结构与解密校验，未切换资料。')
    from companion_core.file_library import verify_files
    from companion_core.input_images import verify_blobs
    from companion_core.media_originals import verify_originals
    from companion_core.sticker_library import verify_stickers

    verify_blobs(store)
    verify_originals(store)
    verify_stickers(store)
    verify_files(store)
    # Every registered asset must exist in the new root and authenticate before any switch.
    asset_transfer.capture(store)
    with store.db() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        staged = postgres_transfer._snapshot(store, db)
    again = portable_validation.validate_snapshot(staged, source_key, store.profile)
    records = _rows(staged['tables']['content_records'], ('kind', 'id'))
    history = _rows(staged['tables']['content_history'], ('kind', 'id', 'version'))
    if not _rows(source['tables']['content_records'], ('kind', 'id')) <= records:
        raise AssistantError('portable_validate', '暂存资料缺少来源记录，未切换资料。')
    if not _rows(source['tables']['content_history'], ('kind', 'id', 'version')) <= history:
        raise AssistantError('portable_validate', '暂存资料缺少来源历史，未切换资料。')
    return {'records': len(records), 'history': len(history), 'ciphertexts': again['ciphertexts'],
            'ledger': again['ledger'],
            'records_added': len(records) - len(source['tables']['content_records']['rows']),
            'history_added': len(history) - len(source['tables']['content_history']['rows'])}


def _keyslot(vault, generation, source_key, credential):
    """The new generation's own slot: the source data key and this cluster's runtime password."""
    raw = json.dumps({'data_key': base64.b64encode(source_key).decode('ascii'),
                      'database_password': credential['database_password']})
    try:
        vault.set_password(VAULT, generation, raw)
        if vault.get_password(VAULT, generation) != raw:
            raise AssistantError('portable_vault', '无法确认新代次凭据写入，未切换资料。')
    except AssistantError:
        raise
    except Exception:
        raise AssistantError('portable_vault', '凭据库不可用，未切换资料。') from None


def _switch_state(root, generation):
    """What the commit point says now: applied, unchanged, or present but unreadable."""
    pointer = root/installation_generation.POINTER
    try:
        raw = pointer.read_bytes()
    except FileNotFoundError:
        return 'unchanged'
    except OSError:
        return 'uncertain'
    try:
        value = json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError):
        return 'uncertain'
    return 'applied' if isinstance(value, dict) and value.get('generation') == generation else 'unchanged'


def _unchanged(store, expected):
    if postgres_transfer.fingerprint(store) != expected:
        raise AssistantError('portable_changed', '当前资料在恢复过程中已变化，未切换资料，请重新预览。')


def _compatible(store, snapshot):
    """The source snapshot must be the schema this application writes, not an older one."""
    with store.db() as db:
        migrations = [list(row) for row in
                      db.execute('SELECT version,checksum FROM schema_migrations ORDER BY version')]
    if snapshot.get('migrations') != migrations:
        raise AssistantError('portable_schema', '备份数据库版本与当前应用不一致，未切换资料。')


def _current_matches(store, selection):
    """The open store must be this installation's selected generation, not a stray database."""
    if Path(store.root).resolve() != Path(selection['store']).resolve():
        raise AssistantError('portable_current', '当前资料目录与代次记录不一致，未切换资料。')
    with store.db() as db:
        database = db.execute('SELECT current_database()').fetchone()[0]
    if database != selection['database']:
        raise AssistantError('portable_current', '当前数据库与代次记录不一致，未切换资料。')


def _stage(root, store, state, blob, source_key, expected, preservation, session,
           source_name, hook, vault, source_mode, generation=None, before_commit=None):
    """The whole staging body; the caller already holds the root and current-store locks."""
    installation = state['installation']
    selection = installation_generation.load_selection(root, installation)
    _current_matches(store, selection)
    settings = _local_settings(store, installation)
    backup_key = _backup_key(source_key)
    snapshot = postgres_transfer.unpack(blob, backup_key)
    portable_validation.validate_snapshot(snapshot, source_key, store.profile)
    _compatible(store, snapshot)
    _preserved(store, preservation)
    _unchanged(store, expected)
    hook('source-verified')
    credential = _base_credential(vault, installation)
    # A trusted host may name the generation instead of accepting a random one, so a staging
    # that is interrupted right after the commit point can still be attributed by its own
    # durable name. The default callers keep the random identifier.
    generation = secrets.token_hex(16) if generation is None else generation
    directory = installation_generation._derived(root, installation_generation.GENERATIONS, generation)
    if directory.exists() or directory.is_symlink():
        raise AssistantError('portable_stage_exists', '暂存代次目录已存在，未切换资料。')
    store_root = installation_generation._derived(
        root, installation_generation.GENERATIONS, generation, installation_generation.STORE)
    try:
        directory.mkdir(parents=True)
        protect_directory(directory)
        store_root.mkdir()
    except (OSError, subprocess.CalledProcessError):
        raise AssistantError('portable_directory_permissions',
                             '无法为暂存代次设置目录权限，未切换资料。') from None
    journal = directory/JOURNAL
    stamp = _stamp(state, source_name, blob, preservation, expected, generation, source_mode)
    _journal(journal, 'created', stamp)
    database = installation_generation.RESTORED_DATABASE_PREFIX+generation
    connection = _maintenance(state, credential)
    try:
        _cluster(connection, root)
        _roles(connection)
        _create_database(connection, database)
        _journal(journal, 'database', {**stamp, 'database': database})
        staged = PostgresStore(store_root, _options(state, credential, database), source_key,
                               profile=store.profile)
        try:
            staged.migrate()
            staged.initialize()
            _grants(database, state, credential)
            _journal(journal, 'schema', {**stamp, 'database': database})
            # What was restored and, separately, the copy of this machine's own data taken
            # just before the switch: two different files, never reported as one.
            receipt = {'id': secrets.token_hex(16), 'status': 'assets_pending',
                       'completed_at': utcnow(), 'role_started': False,
                       'source': source_mode, 'generation': generation,
                       'source_name': stamp['source_backup'],
                       'preservation_name': preservation['name']}
            if stamp['source_backup'] is not None:
                # ``name`` keeps the meaning it has for a local backup restore: the backup
                # that was restored. A source the host did not name is simply absent.
                receipt['name'] = stamp['source_backup']

            def finalize(db):
                source = _source_identity(staged, db)
                _apply_local(staged, settings, db)
                current_owner = ActorRef(**settings[desktop_identity.OWNER]['actor'])
                if source_mode == 'preserved':
                    # This machine's own earlier data keeps exactly the logical identity it
                    # had, including the absence of a binding; the current physical owner,
                    # application lock and local settings were just written above.
                    if (source['installation'] != installation
                            or source['owner'] != current_owner):
                        raise AssistantError('portable_source_identity',
                                             '保全资料不属于本次安装，未切换资料。')
                    desktop_identity.restore_local(staged, role=source['role'].as_dict(),
                                                   existing=source['binding'], db=db)
                else:
                    staged.set_state(desktop_identity.ROLE, source['role'].as_dict(), db)
                    desktop_identity.bind(
                        staged, installation=installation, owner=current_owner,
                        room=source['room'], logical_role=source['role'],
                        source={'installation': source['installation'], 'at': utcnow()}, db=db)
                _hold_session(staged, session, generation, db)
                _retire_tasks(staged, db)
                staged.set_state('desktop-last-restore', receipt, db)
                db.execute("DELETE FROM state WHERE key LIKE 'desktop-request:%'")
                GovernanceRepository(staged).audit(
                    current_owner.key, 'desktop_portable_restore_staged'
                    if source_mode == 'portable' else 'desktop_preservation_restore_staged',
                    generation, db=db)

            postgres_transfer.restore(staged, blob, backup_key,
                                      expected=postgres_transfer.fingerprint(staged), finalize=finalize)
            hook('restored')
            _journal(journal, 'restored', {**stamp, 'database': database})
            staged.set_state('desktop-last-restore', {**receipt, 'status': 'complete'})
            report = _verified(staged, source_key, snapshot)
            hook('validated')
            _journal(journal, 'validated', {**stamp, 'database': database, **report})
            _keyslot(vault, generation, source_key, credential)
            hook('keyslot')
            prepared = installation_generation.prepare_selection(root, installation, generation)
            _journal(journal, 'prepared', {**stamp, 'database': database,
                                           'manifest_sha256': prepared['manifest_sha256']})
            hook('prepared')
            # The current data must still be exactly what the preview authenticated.
            _unchanged(store, expected)
            if before_commit is not None:
                # Last point before the pointer moves: a trusted host records the durable name,
                # the database, the store and the ready manifest of the clone it is about to
                # select, so an interruption right after the commit is still attributable.
                before_commit({'installation': installation, 'generation': generation,
                               'database': database, 'store': str(store_root),
                               'manifest_sha256': prepared['manifest_sha256'],
                               'source_mode': source_mode, 'verified': report})
        finally:
            staged.database.close()
    except BaseException as error:
        code = error.code if isinstance(error, AssistantError) else type(error).__name__
        _best_effort(journal, 'failed', {**stamp, 'database': database}, code)
        raise
    finally:
        connection.close()
    try:
        committed = installation_generation.commit_selection(root, installation, generation, selection)
    except BaseException as error:
        current = _switch_state(root, generation)
        code = error.code if isinstance(error, AssistantError) else type(error).__name__
        _best_effort(journal, 'commit-failed' if current == 'unchanged' else 'commit-uncertain',
                     {**stamp, 'database': database}, code)
        if current == 'unchanged':
            raise
        raise AssistantError(
            'portable_switch_applied',
            '切换步骤已提交，本次结果未完全确认；请按本机代次记录重启确认，不要重复提交整次恢复。') from None
    try:
        hook('committed')
        _journal(journal, 'committed', {**stamp, 'database': database,
                                        'manifest_sha256': committed['manifest_sha256']})
    except BaseException:
        # Past the commit point the switch stands; the restart reads the commit point.
        raise AssistantError(
            'portable_switch_applied',
            '资料切换已完成，请重启应用以使用新代次资料；不要重复提交整次恢复。') from None
    return {'generation': generation, 'switched': True, 'restart_required': True,
            'restored': True, 'database': database, 'store': str(store_root),
            'manifest_sha256': committed['manifest_sha256'], 'verified': report}


def stage_restore(current_store, root, source_blob, source_key, expected_current_fingerprint,
                  preservation, application_session, *, source_name=None, checkpoint=None,
                  vault=None, source_mode='portable', generation=None, before_commit=None):
    """Stage one verified restore and, only when everything passed, select it.

    Call with keywords. ``current_store`` is this installation's open store, ``root`` is the
    installation directory holding ``installation.json``, ``source_blob`` and ``source_key``
    are the authenticated source pair (a portable export or one of this machine's own retained
    copies), ``expected_current_fingerprint`` and ``preservation`` are exactly what the preview
    returned, and ``application_session`` is the running application session whose new runtime
    session holds every platform. ``source_name`` is the trusted display basename of the data
    being restored; it is recorded in the journal and in the new ``desktop-last-restore``
    receipt next to ``preservation_name``, and it may be omitted, because the desktop service
    hands over the authenticated pair only. An unnamed source is then left unnamed: the
    preservation copy of this machine's own data is recorded as that and never as the source.
    ``checkpoint`` is a trusted host hook called at the phases used by fault-injection tests,
    and ``vault`` overrides the Windows credential store only in tests.

    ``source_mode`` is a trusted host decision, never page input: ``'portable'`` (the default)
    adopts the source's logical identities through a new binding for this installation, while
    ``'preserved'`` restores this machine's own earlier logical identity exactly, including the
    absence of a binding, and requires the source owner to be this installation's own physical
    owner. Both keep the current physical owner, application lock and local settings that were
    read from the running store before the snapshot rows were copied.

    ``generation`` and ``before_commit`` are additive hooks for one trusted maintenance caller
    (the local upgrade transaction) and change nothing for the existing ones: a 32-hex
    ``generation`` names the staged generation instead of accepting a random one, and
    ``before_commit`` is called once with that name, the database, the store and the clone's
    ready manifest *after* the clone is prepared and verified and *before* the pointer
    replacement, so the caller can persist them and an interruption right after the commit
    still belongs to exactly one transaction. Neither hook can weaken a check above: both run
    after every existing verification and before the same single pointer replacement.

    A root-wide FileLock serializes staging for one installation while ``current_store.lock``
    freezes writes on the current store. Nothing outside the new generation directory and the
    new credential slot is written, and no database, store, key or earlier generation is
    deleted, here or on any failure path. Returns {generation, switched, restart_required,
    restored, database, store, manifest_sha256, verified}, which satisfies the service's
    stager contract; raises ``portable_switch_applied`` when a failure happened after the
    commit point, which means the restart now reads the new generation and the whole restore
    must not be retried.
    """
    if source_mode not in ('portable', 'preserved'):
        raise AssistantError('portable_input', '恢复来源类型无效，未切换资料。')
    if generation is not None and (not isinstance(generation, str)
                                   or not installation_generation.IDENTIFIER.fullmatch(generation)):
        raise AssistantError('portable_input', '代次标识无效。')
    if before_commit is not None and not callable(before_commit):
        raise AssistantError('portable_input', '提交前回调无效。')
    root = _root(root)
    state = _installation(root)
    blob = _blob(source_blob)
    key = _source_key(source_key)
    expected = _fingerprint(expected_current_fingerprint)
    artifact = _preservation(preservation)
    session = _session(application_session)
    hook = checkpoint if callable(checkpoint) else (lambda phase: None)
    vault = vault if vault is not None else VaultCredentials()
    if not isinstance(current_store, PostgresStore):
        raise AssistantError('portable_input', '当前资料库不可用，未切换资料。')
    lock = FileLock(str(root/LOCK_FILE), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise AssistantError('portable_busy', '另一个资料切换正在进行，请稍后重试。') from None
    except OSError:
        raise AssistantError('portable_lock_failed', '切换锁不可用，未切换资料。') from None
    try:
        current_store.lock.acquire()
    except Timeout:
        lock.release()
        raise AssistantError('portable_busy', '当前资料仍在写入，请停止后重试。') from None
    except OSError:
        lock.release()
        raise AssistantError('portable_lock_failed', '资料锁不可用，未切换资料。') from None
    try:
        return _stage(root, current_store, state, blob, key, expected, artifact, session,
                      source_name, hook, vault, source_mode, generation, before_commit)
    finally:
        current_store.lock.release()
        lock.release()
