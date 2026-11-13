"""Explicit first-install state machine; never import or reset an existing store.

A fresh installation always writes the original generation: database ``qiban`` and store
``<root>/store``. An installation whose journal already says ``ready`` is only
re-validated: it reads its selected generation through
``installation_generation.load_selection``, migrates and authorizes exactly that database
and store, and writes a storage configuration that names that generation. The commit point
``<root>/active-generation.json`` belongs to ``installation_generation`` alone; this module
never writes it, so restarting or repairing an installation cannot point it back at the
original database. A restored generation whose database or store disappeared is refused
with ``installation_generation_missing`` or the generation records' own code instead of
being recreated as an empty store.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import subprocess
import time
import uuid
from pathlib import Path

import installation_generation
import psycopg
from credential_vault import VAULT, VaultCredentials
from filelock import FileLock, Timeout
from install_windows import PasswordPipe, protect_directory, user_sid
from psycopg import sql

from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore
from companion_core.process_scope import ProcessScope
from qiban.desktop_profile import desktop_profile


def atomic(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w', encoding='utf8') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def checkpoint(root, phase, pause, **details):
    atomic(root/'installation-status.json', {'phase': phase, 'pid': os.getpid(), **details})
    if pause == phase:
        # Deterministic fault-injection handoff. A real host kill interrupts this process.
        end = time.monotonic()+60
        while not (root/'continue-test').exists():
            if time.monotonic() > end:
                raise AssistantError('installation_test_timeout', 'Test pause timed out')
            time.sleep(.05)


def owned(command, scope, **kwargs):
    process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW | 0x4, **kwargs)
    scope.attach(process)
    scope.resume()
    return process


def original_settings(root, installation):
    """The original generation's addresses; every fresh installation writes these."""
    return {'generation': None, 'database': installation_generation.ORIGINAL_DATABASE,
            'data': Path(root)/'store', 'credential_id': installation, 'manifest_sha256': None}


def generation_settings(root, installation):
    """Where a ready installation reads and writes; only its trusted root decides this.

    No pointer means the original generation. A pointer is authoritative: a broken record,
    a missing manifest, a mismatched digest or a missing store raises instead of selecting
    an empty original store, and the ready path never runs before the store root exists.
    """
    selection = installation_generation.load_selection(root, installation)
    return {'generation': selection['generation'], 'database': selection['database'],
            'data': Path(selection['store']), 'credential_id': selection['credential_id'],
            'manifest_sha256': selection['manifest_sha256']}


def selected_credential(vault, installation, settings, key, password):
    """Data key and runtime password of the selected generation's credential slot.

    This installation's own slot keeps the bootstrap and the runtime passwords. A restored
    generation keeps its own data key in its own slot, and its runtime password must be this
    cluster's runtime password, because ``qiban_runtime`` is one login for the cluster.
    """
    if settings['credential_id'] == installation:
        return key, password
    try:
        raw = vault.get_password(VAULT, settings['credential_id'])
    except Exception:
        raise AssistantError('storage_vault_unavailable', 'Credential vault unavailable') from None
    if raw is None:
        raise AssistantError('storage_vault_missing', 'Installation credentials missing')
    try:
        value = json.loads(raw)
        restored = base64.b64decode(value['data_key'], validate=True)
        restored_password = value['database_password']
    except (KeyError, TypeError, ValueError):
        raise AssistantError('storage_vault_invalid', 'Invalid installation credentials') from None
    if len(restored) != 32 or not isinstance(restored_password, str) or restored_password != password:
        raise AssistantError('storage_vault_invalid', 'Invalid installation credentials')
    return restored, restored_password


def authorize_runtime(db):
    """Runtime access on one database, in its own transaction.

    Also runs on every start of an existing installation, because a crash after a schema
    commit may have created tables the runtime login has not been granted yet. It never
    widens the runtime login itself: no CREATE, no ownership, no schema_migrations writes.
    """
    with db.transaction():
        db.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        db.execute('GRANT USAGE ON SCHEMA public TO qiban_runtime')
        db.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO qiban_runtime')
        db.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO qiban_runtime')
        db.execute('GRANT UPDATE ON SEQUENCE content_audit_id_seq TO qiban_runtime')
        db.execute('REVOKE INSERT,UPDATE,DELETE ON schema_migrations FROM qiban_runtime')


def storage_configuration(root, state, settings, worker):
    """storage-config.json of the selected generation; the root and account folders stay put."""
    return {'installation': state['installation'], 'credential_id': settings['credential_id'],
            'data': str(settings['data']), 'desktop_management': True,
            'qq_runtime': str(Path(worker).parent.parent/'qq'),
            'database': {'host': '127.0.0.1', 'port': state['port'], 'user': 'qiban_runtime',
                         'dbname': settings['database']}}


def initialize(root, binaries, worker, pause=None):
    root, binaries, worker = Path(root).resolve(), Path(binaries).resolve(), Path(worker).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        protect_directory(root)
    except (OSError, subprocess.CalledProcessError):
        raise AssistantError('installation_directory_permissions', '无法设置本机资料目录权限，请检查目录访问权限。') from None
    try:
        with FileLock(str(root/'installation.lock'), timeout=0):
            return locked_initialize(root, binaries, worker, pause)
    except Timeout:
        raise AssistantError('installation_busy', 'Installation already in progress') from None


def initial_artifact(path):
    if path.name in {'installation.lock', 'installation.json.tmp'}:
        return True
    if not re.fullmatch(r'installation-result-[0-9]+\.json', path.name) or not path.is_file() or path.is_symlink():
        return False
    try:
        value = read(path)
    except (OSError, ValueError):
        return False
    return (isinstance(value, dict) and set(value) == {'error'}
            and value['error'] in ('installation_failed', 'installation_directory_permissions',
                                   'installation_unknown_directory', 'installation_busy'))


def available_port(state, root, binaries, pause):
    """Recover an OS-denied port only when this database is confirmed stopped."""
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', state['port'])) == 0:
            raise AssistantError('database_port_in_use', 'Installation port is in use')
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', state['port']))
    except OSError as error:
        if getattr(error, 'winerror', None) != 10013:
            raise AssistantError('database_port_in_use', 'Installation port unavailable') from None
        status = subprocess.run([str(binaries/'pg_ctl.exe'), '-D', str(root/'database'), 'status'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if status.returncode != 3:
            raise AssistantError('database_status_failed', 'Cannot confirm database is stopped')
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        state = {**state, 'port': port, 'port_recovery': {
            'previous': state['port'], 'reason': 'windows_bind_denied'}}
        atomic(root/'installation.json', state)
        checkpoint(root, 'port-reassigned', pause)
    return state


def locked_initialize(root, binaries, worker, pause):
    # Re-check under the same lock held by the running worker and maintenance.
    from upgrade import gate

    gate.require(root, gate.identity_for_root(root, worker), gate.INITIALIZE)
    journal = root/'installation.json'
    if not journal.exists():
        # Refuse an unknown existing directory; never treat unknown data as disposable.
        if any(not initial_artifact(p) for p in root.iterdir()):
            raise AssistantError('installation_unknown_directory', 'Unknown installation directory')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        state = {'format': 1, 'installation': uuid.uuid4().hex, 'owner': secrets.token_hex(24),
                 'port': port, 'phase': 'planned'}
        atomic(journal, state)
    state = read(journal)
    if state.get('format') != 1 or state.get('phase') not in {'planned','credentials','cluster','schema','ready'}:
        raise AssistantError('installation_state_invalid', 'Invalid installation state')
    if state['phase'] != 'ready' and (root/installation_generation.POINTER).exists():
        # A commit point exists, so this directory already reads a specific generation.
        # The fresh path must never write its configuration over that selection.
        raise AssistantError('installation_state_invalid', '代次记录与安装进度不一致，请人工检查')
    # A ready installation is only re-validated on its selected generation. A fresh one has
    # no store root yet, so it never asks for a selection here.
    settings = (generation_settings(root, state['installation']) if state['phase'] == 'ready'
                else original_settings(root, state['installation']))
    checkpoint(root, 'planned', pause)
    # Every credential access of this application goes through one cross-process mutex per
    # service, so two installers preparing their own slot cannot lose each other's entry.
    vault = VaultCredentials()
    raw = vault.get_password(VAULT, state['installation'])
    if raw is None:
        if state['phase'] != 'planned':
            raise AssistantError('storage_vault_missing', 'Installation credentials missing')
        raw = json.dumps({'data_key': base64.b64encode(secrets.token_bytes(32)).decode(),
                          'database_password': secrets.token_urlsafe(36), 'bootstrap_password': secrets.token_urlsafe(36)})
        vault.set_password(VAULT, state['installation'], raw)
        if vault.get_password(VAULT, state['installation']) != raw:
            raise AssistantError('storage_vault_unavailable', 'Credential verification failed')
    credential = json.loads(raw)
    key = base64.b64decode(credential['data_key'], validate=True)
    if len(key) != 32 or any(len(credential[k]) < 32 for k in ['database_password','bootstrap_password']):
        raise AssistantError('storage_vault_invalid', 'Invalid installation credentials')
    data_key, database_password = selected_credential(
        vault, state['installation'], settings, key, credential['database_password'])
    checkpoint(root, 'vault-written', pause)
    if state['phase'] == 'planned':
        state['phase'] = 'credentials'
        atomic(journal, state)
    data, logs = root/'database', root/'logs'
    logs.mkdir(exist_ok=True)
    complete = root/'cluster-complete.json'
    if not complete.exists():
        if state['phase'] != 'credentials':
            raise AssistantError('installation_cluster_missing', 'Completed cluster missing')
        if data.exists():
            # Only this not-yet-committed install's own directory; retain it for diagnosis.
            abandoned = root/'abandoned'
            abandoned.mkdir(exist_ok=True)
            data.rename(abandoned/('cluster-'+uuid.uuid4().hex))
        pipe, scope = PasswordPipe(), ProcessScope()
        try:
            with (logs/'initdb.log').open('ab') as log:
                process = owned([str(binaries/'initdb.exe'), '-D', str(data), '-U', 'qiban_setup',
                    '--encoding=UTF8', '--locale=C', '--auth=scram-sha-256', '--pwfile='+pipe.name],
                    scope, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                checkpoint(root, 'initdb-running', pause, child=process.pid)
                pipe.send(credential['bootstrap_password'])
                pipe.close()
                if process.wait(timeout=60) != 0:
                    raise AssistantError('installation_initdb_failed', 'Database initialization failed')
        finally:
            pipe.close()
            scope.close()
        checkpoint(root, 'initdb-finished', pause)
        (data/'qiban-probe-owner').write_text(state['owner'], encoding='ascii')
        with (data/'postgresql.conf').open('a', encoding='utf8') as config:
            config.write(f"\nlisten_addresses='127.0.0.1'\nport={state['port']}\nlog_statement='none'\nlog_min_error_statement='panic'\n")
        atomic(complete, {'owner': state['owner']})
    if read(complete) != {'owner': state['owner']} or (data/'qiban-probe-owner').read_text() != state['owner']:
        raise AssistantError('database_owner_mismatch', 'Installation ownership mismatch')
    if (data/'PG_VERSION').read_text().strip() != '18':
        raise AssistantError('database_version_mismatch', 'Installation version mismatch')
    if state['phase'] == 'credentials':
        state['phase'] = 'cluster'
        atomic(journal, state)
    checkpoint(root, 'cluster', pause)
    state = available_port(state, root, binaries, pause)
    options = {'host': '127.0.0.1', 'port': state['port'], 'dbname': 'postgres', 'user': 'qiban_setup',
               'password': credential['bootstrap_password'], 'connect_timeout': 2}
    scope, store = ProcessScope(), None
    try:
        # Do not take over a server or erase its PID file, including after a crash.
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', state['port'])) == 0:
                raise AssistantError('database_port_in_use', 'Installation port is in use')
        with (logs/'bootstrap.log').open('ab') as log:
            server = owned([str(binaries/'postgres.exe'), '-D', str(data), '-p', str(state['port'])], scope,
                           stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            deadline = time.monotonic()+30
            while True:
                if server.poll() is not None or time.monotonic() > deadline:
                    raise AssistantError('installation_database_failed', 'Bootstrap database unavailable')
                try:
                    connection = psycopg.connect(**options, autocommit=True)
                    break
                except psycopg.OperationalError:
                    time.sleep(.1)
            with connection as db:
                directory = Path(db.execute('SHOW data_directory').fetchone()[0]).resolve()
                if directory != data:
                    raise AssistantError('database_owner_mismatch', 'Wrong database directory')
                if state['phase'] != 'ready':
                    with db.transaction():
                        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='qiban_owner'").fetchone():
                            db.execute('CREATE ROLE qiban_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
                        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='qiban_runtime'").fetchone():
                            db.execute(sql.SQL('CREATE ROLE qiban_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(sql.Literal(credential['database_password'])))
                    if not db.execute("SELECT 1 FROM pg_database WHERE datname='qiban'").fetchone():
                        db.execute('CREATE DATABASE qiban OWNER qiban_owner')
                elif settings['generation'] is not None and not db.execute(
                        'SELECT 1 FROM pg_database WHERE datname=%s', (settings['database'],)).fetchone():
                    # An empty database is not a restored generation; never invent one here.
                    raise AssistantError('installation_generation_missing', '代次数据库缺失，请检查资料代次')
            checkpoint(root, 'database-created', pause)
            owner_options = {**options, 'dbname': settings['database']}
            store = PostgresStore(settings['data'], owner_options, data_key, profile=desktop_profile())
            # Verify existing encrypted data before any schema update. A ready
            # installation may require new versioned tables, never initialization.
            if state['phase'] == 'ready':
                store.read('manifest')
            else:
                with store.db() as db:
                    has_records = db.execute("SELECT to_regclass('content_records')").fetchone()[0] is not None
                if has_records and store.exists('manifest'):
                    store.read('manifest')
            store.migrate()
            checkpoint(root, 'schema-migrated', pause)
            if state['phase'] != 'ready':
                if not store.initialize()['ok']:
                    raise AssistantError('storage_integrity', 'Initial records invalid')
                from qiban.desktop_management import provision_owner

                provision_owner(store,state['installation'],user_sid())
                checkpoint(root, 'records-written', pause)
            store.database.close()
            store = None
            # Also run on upgrade/recovery: a crash after migration may have
            # committed new tables but not granted the runtime access to them.
            with psycopg.connect(**owner_options, autocommit=True) as db:
                authorize_runtime(db)
            checkpoint(root, 'schema-authorized', pause)
            if state['phase'] != 'ready':
                state['phase'] = 'schema'
                atomic(journal, state)
            store = PostgresStore(settings['data'], {**owner_options,'user':'qiban_runtime','password':database_password}, data_key, profile=desktop_profile())
            store.read('manifest')
            from qiban.desktop_backups import finish_pending

            finish_pending(store)
            if not store.validate_critical()['ok']:
                raise AssistantError('storage_integrity', 'Initial records invalid')
            store.database.close()
            store = None
            checkpoint(root, 'validated', pause)
            stopped = subprocess.run([str(binaries/'pg_ctl.exe'), '-D', str(data), '-m', 'fast', '-w', '-t', '10', 'stop'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if stopped.returncode or server.wait(timeout=5):
                raise AssistantError('installation_stop_failed', 'Bootstrap database did not stop normally')
    finally:
        if store is not None:
            store.database.close()
        scope.close()
    if state['phase'] == 'ready':
        # A commit point that moved during this run must not be answered with the
        # configuration of its predecessor: report it instead of writing a stale one.
        confirmed = generation_settings(root, state['installation'])
        if confirmed != settings:
            raise AssistantError('installation_selection_changed', '资料代次在检查期间已切换，请重新启动应用')
    atomic(root/'postgres-spec.json', {'BinDirectory': str(binaries), 'DataDirectory': str(data),
        'LogDirectory': str(logs), 'Port': state['port'], 'MajorVersion': '18', 'OwnerToken': state['owner']})
    atomic(root/'storage-config.json', storage_configuration(root, state, settings, worker))
    atomic(root/'storage-spec.json', {'WorkerExecutable':str(worker), 'ConfigurationPath':str(root/'storage-config.json'),
        'LogDirectory':str(logs)})
    atomic(root/'stack-spec.json', {'PostgresSpecPath':str(root/'postgres-spec.json'), 'StorageSpecPath':str(root/'storage-spec.json')})
    checkpoint(root, 'configs-written', pause)
    state['phase'] = 'ready'
    atomic(journal, state)
    checkpoint(root, 'ready', pause)
    return {'ready': True, 'installation': state['installation']}
