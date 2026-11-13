"""The data side of an upgrade, assembled only from the engines that already exist.

Nothing here copies rows, migrates tables or invents a backup format. The steps are the ones the
portable-restore path already proves:

* a short supervised PostgreSQL server of this root's own (never a server somebody else started),
  started as the installer's trusted ``qiban_setup`` role and stopped again - or, when a server
  already runs, a connection to it only after its data directory is proven to be this root's;
* the current generation's preservation copy written by the existing writer
  (``DesktopPortable._preserve``) and read back through the existing retained-copy provider
  (``portable_preservation``);
* the clone produced by the existing ``portable_restore.stage_restore(..., source_mode='preserved')``
  with two additive parameters only: an explicit generation id and one trusted callback invoked
  after the clone's ready manifest is registered and before the pointer replacement;
* the local-only whitelist and recovery rules the restore path already applies
  (``portable_restore._local_settings`` / ``_apply_local`` / ``_hold_session`` / ``_retire_tasks``).

Two boundaries are kept strictly apart, because they are what a cross-schema rollback turns on:

* the store a session opens is named explicitly. A step executed by the previous program opens
  the *fallback* generation with the previous program's own profile and never touches the new
  schema, while the step executed by the new program opens the clone with the current profile;
* this machine's own whitelist (owner, application lock, local selections) is only carried
  between those two steps as ciphertext, encrypted with the fallback generation's own key.

The one thing an old program cannot do is snapshot data written by a newer schema: its fixed
table list would silently drop the newer tables. That is why a rollback is a cooperation - the
new program preserves the current data and seals the whitelist, and only then may the previous
program switch the pointer back.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import installation
import installation_generation
import psycopg
from credential_vault import VAULT, VaultCredentials
from portable_preservation import preserved_provider
from portable_restore import _apply_local, _hold_session, _local_settings, _retire_tasks, stage_restore
from psycopg import sql as psql

from companion_core import postgres_transfer
from companion_core.cipher import ContentCipher
from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore
from companion_core.process_scope import ProcessScope
from qiban.desktop_portable import DesktopPortable
from qiban.desktop_profile import desktop_profile

from . import journal

MAX_SESSION = 120
IMMEDIATE = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
ORIGINAL_DATABASE = 'qiban'
RESTORED_PREFIX = installation_generation.RESTORED_DATABASE_PREFIX
SYSTEM_DATABASES = frozenset({'postgres', 'template0', 'template1'})
# Injected by a trusted host or an isolated integration run; None means the payload's own bin.
BINARIES = None
SEAL_KIND = 'upgrade-receipt'
SEAL_VERSION = '1'


def _refuse(code, message):
    raise AssistantError(code, message)


def state(root):
    """The ready installation record of this root; nothing is maintained before it is ready."""
    try:
        value = json.loads((Path(root)/'installation.json').read_text(encoding='utf8'))
    except (OSError, ValueError):
        _refuse('upgrade_state', '安装记录无法读取，未继续维护操作。')
    if (not isinstance(value, dict) or value.get('format') != 1
            or not isinstance(value.get('installation'), str)
            or not journal.IDENTIFIER.fullmatch(value['installation'])
            or type(value.get('port')) is not int or not 1024 <= value['port'] <= 65535
            or value.get('phase') != 'ready'):
        _refuse('upgrade_state', '安装记录不是已就绪的安装，未继续维护操作。')
    return value


def credentials(vault, installation_id):
    """This installation's own slot: data key, runtime password and bootstrap password."""
    try:
        raw = vault.get_password(VAULT, installation_id)
    except Exception:
        _refuse('upgrade_vault', '本机凭据库不可用，未继续维护操作。')
    if raw is None:
        _refuse('upgrade_vault', '本次安装的凭据缺失，未继续维护操作。')
    try:
        value = json.loads(raw)
        key = base64.b64decode(value['data_key'], validate=True)
        runtime, bootstrap = value['database_password'], value['bootstrap_password']
    except (KeyError, TypeError, ValueError):
        _refuse('upgrade_vault', '本次安装的凭据无效，未继续维护操作。')
    if (len(key) != 32 or not isinstance(runtime, str) or len(runtime) < 32
            or not isinstance(bootstrap, str) or len(bootstrap) < 32):
        _refuse('upgrade_vault', '本次安装的凭据无效，未继续维护操作。')
    return {'data_key': key, 'database_password': runtime, 'bootstrap_password': bootstrap}


def selected(root, installation_id):
    """The generation this root really selects - never assumed to be the original one."""
    return installation_generation.load_selection(root, installation_id)


def slot_key(vault, installation_id, keyref):
    """The data key of one credential slot: this installation's own, or one generation's own."""
    credential = credentials(vault, installation_id)
    key, _ = installation.selected_credential(vault, installation_id, {'credential_id': keyref},
                                              credential['data_key'],
                                              credential['database_password'])
    return key


def generation_for(installation_id, transaction):
    """The clone's durable id, derived from the transaction so a crash never needs a scan."""
    if not isinstance(transaction, str) or not journal.IDENTIFIER.fullmatch(transaction):
        _refuse('upgrade_state', '事务标识无效。')
    raw = f'qiban-upgrade-clone:{installation_id}:{transaction}'.encode('utf8')
    return hashlib.sha256(raw).hexdigest()[:32]


def plan_clone(installation_id, transaction):
    generation = generation_for(installation_id, transaction)
    return {'generation': generation, 'database': RESTORED_PREFIX+generation,
            'store': 'generations/'+generation+'/store', 'credential_id': generation}


def store_root(root, store):
    """Absolute store path from a journal-relative one; derived, never caller supplied."""
    parts = store.split('/')
    if parts == ['store']:
        return Path(root)/'store'
    if (len(parts) == 3 and parts[0] == 'generations' and parts[2] == 'store'
            and journal.IDENTIFIER.fullmatch(parts[1])):
        return Path(root)/'generations'/parts[1]/'store'
    _refuse('upgrade_journal_invalid', '升级记录中的资料目录无效。')


def options(state_value, credential, database):
    return {'host': '127.0.0.1', 'port': state_value['port'], 'dbname': database,
            'user': 'qiban_setup', 'password': credential['bootstrap_password'],
            'connect_timeout': 5}


def free_port(state_value):
    with socket.socket() as probe:
        return probe.connect_ex(('127.0.0.1', state_value['port'])) != 0


def _binaries(app):
    """The PostgreSQL binaries of this maintenance step.

    A trusted host may inject a read-only bin directory (`BINARIES`) instead of an assembled
    payload - the isolated source-level integration uses the published 0.9.4.175 binaries that
    way, so it never copies a 1.3 GB payload just to start a database.
    """
    binaries = Path(BINARIES) if BINARIES else Path(app)/'resources'/'postgres'/'bin'
    if not (binaries/'postgres.exe').is_file() or not (binaries/'pg_ctl.exe').is_file():
        _refuse('upgrade_database', '程序目录缺少数据库组件，未继续维护操作。')
    return binaries


@contextmanager
def _server(root, app, state_value):
    """A server this call started, and therefore owns; never a server somebody else started."""
    binaries = _binaries(app)
    if not free_port(state_value):
        _refuse('upgrade_database_busy', '本机数据库仍在运行，请先在栖伴窗口中正常退出后再继续。')
    logs = Path(root)/'logs'
    logs.mkdir(exist_ok=True)
    scope, server = ProcessScope(), None
    try:
        with (logs/'upgrade-maintenance.log').open('ab') as log:
            server = installation.owned(
                [str(binaries/'postgres.exe'), '-D', str(Path(root)/'database'),
                 '-p', str(state_value['port'])], scope, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log)
        yield server
    finally:
        if server is not None:
            try:
                subprocess.run([str(binaries/'pg_ctl.exe'), '-D', str(Path(root)/'database'),
                                '-m', 'fast', '-w', '-t', '10', 'stop'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                               creationflags=IMMEDIATE)
                server.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                scope.close()
                _refuse('upgrade_stop_failed', '数据库未能正常停止，请人工确认后再继续。')
        scope.close()


@contextmanager
def session(root, app, *, vault=None, profile=None, selection=None, store_factory=None,
            checkpoint=None):
    """Own supervised server plus one generation's store, opened as trusted `qiban_setup`.

    ``selection`` names the generation this step is allowed to touch and defaults to the current
    selection. ``profile`` is the schema the executing program understands, so a step run by the
    previous program opens the previous generation with the previous profile and can never reach
    the newer schema behind the caller's back.
    """
    root = Path(root)
    vault = vault if vault is not None else VaultCredentials()
    create = store_factory if store_factory is not None else PostgresStore
    profile = profile if profile is not None else desktop_profile()
    value = state(root)
    credential = credentials(vault, value['installation'])
    descriptor = selected(root, value['installation']) if selection is None else selection
    key = slot_key(vault, value['installation'], descriptor['credential_id'])
    store = None
    with _server(root, app, value):
        store = create(store_root(root, journal.selection(descriptor)['store']),
                       options(value, credential, descriptor['database']), key, profile=profile)
        try:
            deadline = time.monotonic()+30
            while True:
                try:
                    with store.db():
                        pass
                    break
                except psycopg.OperationalError:
                    if time.monotonic() > deadline:
                        _refuse('upgrade_database_failed', '无法启动本机数据库，未继续维护操作。')
                    time.sleep(.1)
            if checkpoint is not None:
                checkpoint('session-ready')
            yield store, key, value, credential, descriptor
        finally:
            store.database.close()


@contextmanager
def live(root, *, vault=None, profile=None, store_factory=None, selection=None):
    """One generation over the server that is already running, proven to belong to this root."""
    root = Path(root)
    vault = vault if vault is not None else VaultCredentials()
    create = store_factory if store_factory is not None else PostgresStore
    profile = profile if profile is not None else desktop_profile()
    value = state(root)
    credential = credentials(vault, value['installation'])
    descriptor = selected(root, value['installation']) if selection is None else selection
    key = slot_key(vault, value['installation'], descriptor['credential_id'])
    try:
        with psycopg.connect(**options(value, credential, descriptor['database'])) as probe:
            actual = Path(probe.execute('SHOW data_directory').fetchone()[0]).resolve()
    except (psycopg.Error, TypeError):
        _refuse('upgrade_database', '本机数据库不可用，未继续维护操作。')
    if actual != (root/'database').resolve():
        _refuse('upgrade_database', '当前数据库不属于本次安装，未继续维护操作。')
    store = create(store_root(root, journal.selection(descriptor)['store']),
                   options(value, credential, descriptor['database']), key, profile=profile)
    try:
        yield store, key, value, credential, descriptor
    finally:
        store.database.close()


@contextmanager
def admin(root, *, vault=None, app=None):
    """Autocommit maintenance connection to `postgres`: from a server we own, or one we verified."""
    root = Path(root)
    vault = vault if vault is not None else VaultCredentials()
    value = state(root)
    credential = credentials(vault, value['installation'])
    options_value = options(value, credential, 'postgres')

    def connect():
        return psycopg.connect(**options_value, autocommit=True)

    def proven():
        try:
            with connect() as connection:
                actual = Path(connection.execute('SHOW data_directory').fetchone()[0]).resolve()
        except (psycopg.Error, TypeError):
            _refuse('upgrade_database', '本机数据库不可用，未继续维护操作。')
        if actual != (root/'database').resolve():
            _refuse('upgrade_database', '当前数据库不属于本次安装，未继续维护操作。')

    if free_port(value):
        if app is None:
            _refuse('upgrade_database', '维护数据库未运行且没有可用于启动的程序目录。')
        with _server(root, app, value):
            deadline = time.monotonic()+30
            while True:
                try:
                    proven()
                    break
                except AssistantError as error:
                    if error.code != 'upgrade_database':
                        raise
                    if time.monotonic() > deadline:
                        _refuse('upgrade_database_failed', '无法启动本机数据库，未继续维护操作。')
                    time.sleep(.1)
            yield SimpleNamespace(connect=connect, state=value, credential=credential,
                                  root=root, owned=True)
        return
    proven()
    yield SimpleNamespace(connect=connect, state=value, credential=credential, root=root,
                          owned=False)


def drop_database(session_value, database):
    """Drop one database this installation's own upgrade created; verify ownership and identity.

    Only a generation database (or the original one) that is neither the current selection nor a
    system database, owned by the installer's own ``qiban_owner`` role, may be dropped; the
    caller must already have refused anything the open record still references.
    """
    if not isinstance(database, str) or database in SYSTEM_DATABASES:
        _refuse('upgrade_state', '不允许清理系统数据库。')
    if database != ORIGINAL_DATABASE and not (
            database.startswith(RESTORED_PREFIX)
            and journal.IDENTIFIER.fullmatch(database[len(RESTORED_PREFIX):])):
        _refuse('upgrade_state', '只允许清理升级自己建立的代次数据库。')
    root = session_value.root
    installation_id = session_value.state['installation']
    current = installation_generation.load_selection(root, installation_id)
    if current['database'] == database:
        _refuse('upgrade_state', '不允许清理当前正在使用的代次数据库。')
    with session_value.connect() as connection:
        row = connection.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s',
                                 (database,)).fetchone()
        if row is None:
            return {'database': database, 'dropped': False, 'reason': '不存在'}
        if row[0] != 'qiban_owner':
            _refuse('upgrade_database', '该数据库不属于本次安装的属主，未清理。')
        connection.execute(psql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
            psql.Identifier(database)))
        remaining = connection.execute('SELECT 1 FROM pg_database WHERE datname=%s',
                                       (database,)).fetchone()
    if remaining:
        _refuse('upgrade_database', '数据库未能删除，未继续清理。')
    return {'database': database, 'dropped': True}


def preserve(store, key):
    """One preservation copy, written and re-read by the existing portable preserve writer."""
    portable = DesktopPortable(SimpleNamespace(store=store), lambda: key, _no_stager)
    return portable._preserve()


def _no_stager(*_):
    _refuse('upgrade_state', '维护过程不执行资料切换。')


def fingerprint(store):
    return postgres_transfer.fingerprint(store)


def migrations(store):
    """The data version as a digest of the install's own (version, checksum) list."""
    with store.db() as db:
        rows = [[int(row[0]), str(row[1])] for row in
                db.execute('SELECT version,checksum FROM schema_migrations ORDER BY version')]
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode('utf8')).hexdigest()


def preservation_keyref(installation_id, preservation):
    """Which credential slot can still decrypt one preservation artifact."""
    return installation_id if preservation['generation'] is None else preservation['generation']


def preserved_pair(root, installation_id, vault, preservation):
    """Bytes and data key of a preservation artifact, through the existing retained-copy provider."""
    provider = preserved_provider(
        root, installation_id, lambda keyref: slot_key(vault, installation_id, keyref))
    entry = provider.reopen(preservation_keyref(installation_id, preservation),
                            preservation['name'])
    if hashlib.sha256(entry['blob']).hexdigest() != preservation['sha256']:
        _refuse('upgrade_preserve', '升级前的保全备份校验失败，未继续维护操作。')
    if len(entry['blob']) != preservation['bytes']:
        _refuse('upgrade_preserve', '升级前的保全备份大小已变化，未继续维护操作。')
    return entry['blob'], entry['key']


def verify_artifact(root, preservation, *, key):
    """Prove one preservation artifact is intact and decryptable with the slot that owns it."""
    store = store_root(root, preservation['store'])
    folder = store/'history'/'portable-preservation'
    path = folder/preservation['name']
    try:
        details = path.lstat()
    except OSError:
        _refuse('upgrade_preserve', '保全备份已不存在，未继续维护操作。')
    if path.is_symlink() or not path.is_file() or details.st_size != preservation['bytes']:
        _refuse('upgrade_preserve', '保全备份已改变，未继续维护操作。')
    if path.parent.resolve() != folder.resolve():
        _refuse('upgrade_preserve', '保全备份地址不可信，未继续维护操作。')
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != preservation['sha256']:
        _refuse('upgrade_preserve', '保全备份校验失败，未继续维护操作。')
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    backup_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                      info=b'companion-postgres-backup-v1').derive(key)
    postgres_transfer.unpack(blob, backup_key)
    return path


def clone(root, store, *, installation_id, transaction, preservation, blob, source_key,
          expected_fingerprint, application_session, before_commit, vault=None, checkpoint=None):
    """Stage and switch one clone through the existing restore path, with a durable name."""
    _session(application_session)
    plan = plan_clone(installation_id, transaction)
    try:
        report = stage_restore(
            current_store=store, root=root, source_blob=blob, source_key=source_key,
            expected_current_fingerprint=expected_fingerprint,
            preservation={'name': preservation['name'], 'sha256': preservation['sha256'],
                          'bytes': preservation['bytes']},
            application_session=application_session, source_mode='preserved',
            generation=plan['generation'], before_commit=before_commit, checkpoint=checkpoint,
            vault=vault if vault is not None else VaultCredentials())
    except AssistantError as error:
        if error.code == 'portable_switch_applied':
            return {'applied': True, 'uncertain': True, 'code': error.code, 'plan': plan}
        raise
    return {'applied': True, 'uncertain': False, 'report': report, 'plan': plan}


def _session(value):
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SESSION:
        _refuse('upgrade_state', '维护会话标识无效。')


def local_settings(store, installation_id):
    """The existing local-only whitelist: owner, application lock and this machine's selections."""
    return _local_settings(store, installation_id)


def apply_recovery(store, settings, *, application_session, generation, db):
    """Apply that whitelist and the existing restore rules (platforms held, old tasks retired)."""
    _apply_local(store, settings, db)
    _hold_session(store, application_session, generation, db)
    _retire_tasks(store, db)


def seal_settings(settings, key, *, keyref):
    """Encrypt this machine's whitelist with the fallback generation's own key."""
    identifier = secrets.token_hex(16)
    raw = json.dumps(settings, ensure_ascii=False, sort_keys=True).encode('utf8')
    cipher = ContentCipher(key).encrypt(raw, SEAL_KIND, identifier, SEAL_VERSION)
    return {'format': 1, 'keyref': keyref, 'identifier': identifier,
            'ciphertext': base64.b64encode(cipher).decode('ascii')}


def unseal_settings(value, key):
    """Decrypt and strictly check one sealed whitelist; a tampered or foreign receipt is refused."""
    if (not isinstance(value, dict)
            or set(value) != {'format', 'keyref', 'identifier', 'ciphertext'}
            or type(value['format']) is not int or value['format'] != 1
            or not isinstance(value['identifier'], str)
            or not journal.IDENTIFIER.fullmatch(value['identifier'])):
        _refuse('upgrade_state', '本机设置收据无效，未继续回退。')
    try:
        cipher = base64.b64decode(value['ciphertext'], validate=True)
        raw = ContentCipher(key).decrypt(cipher, SEAL_KIND, value['identifier'], SEAL_VERSION)
        settings = json.loads(raw.decode('utf8'))
    except (TypeError, ValueError, UnicodeError, AssistantError):
        _refuse('upgrade_state', '本机设置收据无法解开，未继续回退。')
    if not isinstance(settings, dict) or not settings:
        _refuse('upgrade_state', '本机设置收据内容无效，未继续回退。')
    return settings


def commit_selection(root, installation_id, generation, current):
    """Point the root at one generation that is already prepared; None means the original."""
    expected = {'installation': installation_id, 'generation': current['generation']}
    return installation_generation.commit_selection(root, installation_id, generation, expected)


def selection_generation(root, installation_id):
    """The generation id currently selected, or None for the original generation."""
    return selected(root, installation_id)['generation']
