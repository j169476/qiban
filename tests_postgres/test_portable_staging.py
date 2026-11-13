"""Cross-machine staging against a disposable desktop installation, opt-in only.

This suite really stages a restore: it creates its own databases in the cluster that
``QIBAN_DESKTOP_TEST_ROOT`` belongs to, keeps that installation's original database and
store untouched, and drives ``portable_restore.stage_restore`` end to end. It therefore
needs an explicit runner entry and never runs as part of the ordinary suite:

    QIBAN_DESKTOP_TEST_ROOT=<disposable installation root> QIBAN_PORTABLE_STAGING=1 \
        python -m pytest tests_postgres/test_portable_staging.py -q

The root supplies its identity journal, its credential slot, its cluster port and its own
data directory, so the restore's "same cluster, same directory" checks are exercised for
real. The test never opens the running application's profile and never writes the original
database; it removes the databases, generation directories, credential slots, preservation
file, configuration file and commit point it created.

Databases created here, all named from fresh identifiers:

    qiban_source_<32 hex>    the source machine's full desktop-profile store (exported)
    qiban_restore_<32 hex>   the generation ``stage_restore`` creates and switches to

The current store is a schema-isolated store inside the installation's original database,
so the restore's "current store is the selected generation" checks run against a real
store without touching the rows the disposable installation itself uses.
"""
import base64
import hashlib
import json
import os
import secrets
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from keyring.backends.Windows import WinVaultKeyring
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'desktop_probe'))

import installation  # noqa: E402
import installation_generation  # noqa: E402
import portable_restore  # noqa: E402
import storage_worker  # noqa: E402

from companion_core import postgres_transfer  # noqa: E402
from companion_core.contracts import ActorRef, stable_key  # noqa: E402
from companion_core.errors import AssistantError  # noqa: E402
from companion_core.postgres import PostgresStore  # noqa: E402
from companion_core.runtime_keys import DESKTOP_PLATFORMS, RUN_KEYS  # noqa: E402
from qiban import desktop_identity, desktop_wechat  # noqa: E402
from qiban.desktop_management import LOCK, OWNER, DesktopManagement, provision_owner  # noqa: E402
from qiban.desktop_portable import PRESERVATION, DesktopPortable  # noqa: E402
from qiban.desktop_profile import desktop_profile  # noqa: E402
from qiban.desktop_runtime import HOLD, PREFERENCES, SESSION, DesktopRuntime  # noqa: E402

SESSION_ID = 'portable-staging-session-1'
WINDOWS_SID = 'S-1-5-21-test-owner'
LOCAL_SIWC = {'host': 'local-host', 'session': {'refresh_token': 'LOCAL-ONLY-VALUE'}}
LOCAL_QQ = {'revision': 4, 'selected': {'uin': '10001', 'uid': 'local-uid'}}


def now():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture(scope='module', autouse=True)
def require_entry():
    if not os.environ.get('QIBAN_DESKTOP_TEST_ROOT'):
        pytest.skip('Requires a disposable desktop test installation (QIBAN_DESKTOP_TEST_ROOT)')
    if os.environ.get('QIBAN_PORTABLE_STAGING') != '1':
        pytest.skip('Full staging is opt-in (QIBAN_PORTABLE_STAGING=1)')


@pytest.fixture(scope='module')
def root(require_entry):
    return Path(os.environ['QIBAN_DESKTOP_TEST_ROOT']).resolve()


def cluster(root):
    """The installation journal and the cluster's maintenance credential."""
    state = json.loads((root/'installation.json').read_text(encoding='utf8'))
    credential = json.loads(WinVaultKeyring().get_password(storage_worker.VAULT, state['installation']))
    return state, credential


def options(state, credential, dbname='qiban', schema=None):
    values = {'host': '127.0.0.1', 'port': state['port'], 'user': 'qiban_setup',
              'password': credential['bootstrap_password'], 'dbname': dbname}
    return {**values, 'options': '-c search_path=' + schema} if schema else values


def databases(root):
    state, credential = cluster(root)
    with psycopg.connect(**options(state, credential, 'postgres'), autocommit=True) as db:
        return {row[0] for row in db.execute('SELECT datname FROM pg_database')}


def generations(root):
    folder = root/'generations'
    return {path.name for path in folder.iterdir()} if folder.is_dir() else set()


class Adapter:
    """Synthetic local connector: records stops, never contacts a platform."""

    platform = 'local'

    def __init__(self):
        self.account, self.stopped = 'synthetic-local', []

    def capabilities(self):
        return []

    def runtime_stopped(self, generation):
        self.stopped.append(generation)


class Staging:
    """One staging attempt and everything it may leave behind, cleaned up afterwards."""

    def __init__(self, root):
        self.root = root
        # This suite changes the generation pointer; only the explicitly prepared
        # isolated runner root may be used, never an ordinary development profile.
        marker = json.loads((root/'portable-staging-test.json').read_text(encoding='utf8'))
        assert marker == {'format': 1, 'installation': cluster(root)[0]['installation']}
        self.library = WinVaultKeyring()
        self.pointer = root/'active-generation.json'
        self.saved_pointer = self.pointer.read_bytes() if self.pointer.exists() else None
        self.before_databases = databases(root)
        self.before_generations = generations(root)
        self.state, self.credential = cluster(root)
        self.installation = self.state['installation']
        self.folder = root/'store'/'history'/PRESERVATION
        self.before_preservation = set(self.folder.glob('*.ccbackup')) if self.folder.is_dir() else set()
        self.files = []

    def cleanup(self):
        state, credential = cluster(self.root)
        with psycopg.connect(**options(state, credential, 'postgres'), autocommit=True) as db:
            for name in sorted(databases(self.root) - self.before_databases):
                if name.startswith(('qiban_source_', 'qiban_restore_')):
                    db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        for generation in generations(self.root) - self.before_generations:
            assert installation_generation.IDENTIFIER.fullmatch(generation)
            directory = (self.root/'generations'/generation).resolve()
            assert directory.parent == (self.root/'generations').resolve()
            assert directory.is_relative_to(self.root.resolve())
            shutil.rmtree(directory)
            if self.library.get_password(storage_worker.VAULT, generation) is not None:
                self.library.delete_password(storage_worker.VAULT, generation)
        if self.folder.is_dir():
            for path in set(self.folder.glob('*.ccbackup')) - self.before_preservation:
                path.unlink(missing_ok=True)
        for path in self.files:
            Path(path).unlink(missing_ok=True)
        if self.saved_pointer is None:
            self.pointer.unlink(missing_ok=True)
        else:
            self.pointer.write_bytes(self.saved_pointer)


@pytest.fixture
def staging(root):
    attempt = Staging(root)
    try:
        yield attempt
    finally:
        attempt.cleanup()


class Machine:
    """A full desktop-profile source machine plus this installation's current store."""

    def __init__(self, staging, tmp_path):
        self.staging, self.root = staging, staging.root
        self.state, self.credential = staging.state, staging.credential
        self.installation = staging.installation
        self.profile = desktop_profile()
        self.source_database = 'qiban_source_' + uuid.uuid4().hex
        with psycopg.connect(**options(self.state, self.credential, 'postgres'), autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(
                sql.Identifier(self.source_database), sql.Identifier('qiban_owner')))
        self.source_key = secrets.token_bytes(32)
        self.current_key = base64.b64decode(self.credential['data_key'], validate=True)
        self.source = PostgresStore(Path(tmp_path)/'source', self.connect(self.source_database),
                                    self.source_key, profile=self.profile)
        self.source.migrate()
        self.source.initialize()
        self.schema = 'portable_current_' + uuid.uuid4().hex
        with psycopg.connect(**options(self.state, self.credential), autocommit=True) as db:
            db.execute('CREATE SCHEMA ' + self.schema)
        self.current = PostgresStore(self.root/'store', self.connect('qiban', self.schema),
                                     self.current_key, profile=self.profile)
        self.current.migrate()
        self.current.initialize()
        provision_owner(self.current, self.installation, WINDOWS_SID)
        self.seed_source()
        self.seed_current()

    def connect(self, dbname, schema=None):
        return options(self.state, self.credential, dbname, schema)

    def close(self):
        for store in (self.source, self.current):
            store.database.close()
        with psycopg.connect(**options(self.state, self.credential), autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    # -- fixtures ---------------------------------------------------------------
    def seed_source(self):
        self.source_installation = uuid.uuid4().hex
        self.source_owner = ActorRef('local', uuid.uuid4().hex, self.source_installation)
        self.source_role = ActorRef('qiban-role', uuid.uuid4().hex, self.source_installation)
        self.source.set_state(OWNER, {'format': 1, 'installation': self.source_installation,
                                      'windows_sid': 'S-1-5-21-source-machine',
                                      'actor': self.source_owner.as_dict()})
        self.source.set_state(desktop_identity.ROLE, self.source_role.as_dict())
        self.source.set_state(PREFERENCES, {'revision': 'source-preference',
                                           'platforms': {p: True for p in DESKTOP_PLATFORMS},
                                           'accounts': {p: [] for p in DESKTOP_PLATFORMS}})
        self.source.set_state('core-run:local', {'status': 'active', 'owner': 'source-runtime',
                                                'generation': uuid.uuid4().hex, 'accounts': [],
                                                'read_only': False, 'started_at': now()})
        self.source.set_state(SESSION, {'application': 'source-app', 'instance': 'source', 'held': []})
        # A source machine's own login state and legacy WeChat setup must not arrive here.
        self.source.set_state('desktop-siwc-validation', {'host': 'source-host'})
        self.source.set_state(desktop_wechat.SETUP, {'directory': 'C:\\source', 'identity': 'source-wx'})
        self.source.set_state(stable_key('desktop-wechat-selection', self.source_installation),
                              {'revision': 9, 'selected': 'source-wx', 'accounts': ['source-wx']})
        first = {'schema_version': 1, 'id': 'source-memory', 'kind': 'user_preference',
                 'content': '来源机器的旧记忆', 'confidence': 'observed', 'tags': [], 'locked': False,
                 'source': {'type': 'user_instruction', 'reference': 'source-machine',
                            'recorded_at': now()}}
        self.source.write('memories', first, None, self.source_owner.key, 'source-memory')
        version = self.source.version('memories', 'source-memory')
        self.source.write('memories', {**first, 'content': '来源机器的旧记忆（续写）'}, version,
                          self.source_owner.key, 'source-memory')
        self.source_manifest = self.source.read('manifest')['data']

    def seed_current(self):
        self.current.set_state(LOCK, {'enabled': False, 'revision': 7, 'attempts': 0, 'until': 0})
        self.current.set_state(PREFERENCES, {'revision': 'local-preference',
                                            'platforms': {p: True for p in DESKTOP_PLATFORMS},
                                            'accounts': {p: [] for p in DESKTOP_PLATFORMS}})
        self.current.set_state('desktop-siwc-validation', LOCAL_SIWC)
        self.current.set_state('desktop-qq-account', LOCAL_QQ)
        self.wechat_selection = {'revision': 3, 'selected': 'local-wx', 'accounts': ['local-wx']}
        self.current.set_state(stable_key('desktop-wechat-selection', self.installation),
                               self.wechat_selection)

    def exported(self):
        """The source machine's portable pair, as the service's export writes it."""
        return postgres_transfer.create(self.source, DesktopPortable._backup_key(self.source_key))

    def preserved(self, store=None, key=None):
        """The service's own preservation artifact for one store (the current one by default)."""
        target = self.current if store is None else store
        value = self.current_key if key is None else key
        portable = DesktopPortable(SimpleNamespace(store=target), lambda: value, lambda **pair: {})
        with target.lock:
            preservation = portable._preserve()
            fingerprint = postgres_transfer.fingerprint(target)
        return preservation, fingerprint

    def staged(self, checkpoint=None, source_name=None, store=None, blob=None, key=None,
               current_key=None, preservation=None, fingerprint=None, source_mode='portable'):
        """One staging attempt from either the original store or a selected generation.

        Two keys are involved and never share a default: ``key`` decrypts the incoming
        ``blob`` (a portable export or one retained copy), while ``current_key`` is the data
        key of ``store`` — the store that is preserved first and that the stager reads its
        local settings from. A portable import arrives with the source machine's key and
        preserves this machine's original key; restoring a retained copy of the selected
        generation arrives with the key that wrote that copy and preserves the generation's
        own key.
        """
        target = self.current if store is None else store
        incoming = self.source_key if key is None else key
        current = (self.current_key if store is None else self.source_key) if current_key is None else current_key
        if blob is None:
            blob = self.exported()
        if preservation is None or fingerprint is None:
            fresh, expected = self.preserved(store=target, key=current)
            preservation = preservation or fresh
            fingerprint = fingerprint or expected
        report = portable_restore.stage_restore(
            current_store=target, root=self.root, source_blob=blob,
            source_key=incoming, expected_current_fingerprint=fingerprint,
            preservation=preservation, application_session=SESSION_ID,
            source_name=source_name, checkpoint=checkpoint, source_mode=source_mode)
        return report, preservation, fingerprint

    def generation(self):
        """The selected generation of this installation, read from the trusted root."""
        return installation.generation_settings(self.root, self.installation)

    def opened(self):
        """Open the selected generation through the worker's own configuration path."""
        settings = self.generation()
        config = installation.storage_configuration(
            self.root, self.state, settings, Path('C:/app/resources/storage/Qiban.Storage.exe'))
        path = self.root/'portable-storage-config.json'
        path.write_text(json.dumps(config), encoding='utf8')
        self.staging.files.append(path)
        return settings, storage_worker.open_store(path)


@pytest.fixture
def machine(staging, tmp_path):
    instance = Machine(staging, tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def test_stage_restore_switches_to_a_new_generation_and_keeps_this_machine(machine):
    result, preservation, fingerprint = machine.staged(source_name='kit.ccbackup')
    assert result['switched'] is True and result['restored'] is True
    assert result['restart_required'] is True
    generation = result['generation']
    assert len(generation) == 32
    selection = installation_generation.load_selection(machine.root, machine.installation)
    assert selection['generation'] == generation
    assert selection['database'] == 'qiban_restore_' + generation
    assert result['database'] == selection['database']
    assert Path(result['store']) == Path(selection['store'])
    # The original database, store and preservation artifact are all still there.
    assert (machine.root/'store').is_dir()
    state, credential = cluster(machine.root)
    with psycopg.connect(**options(state, credential, 'postgres')) as db:
        assert Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() == (machine.root/'database').resolve()
    assert postgres_transfer.fingerprint(machine.current) == fingerprint
    artifact = machine.root/'store'/'history'/PRESERVATION/preservation['name']
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == preservation['sha256']
    # The journal holds non-secret bookkeeping only, and never the source key.
    journal = (Path(result['store']).parent/'restore-journal.json').read_bytes()
    record = json.loads(journal.decode('utf8'))
    assert record['phase'] == 'committed' and record['database'] == selection['database']
    # The journal keeps the two names apart the same way the receipt does: the source the
    # host named for this attempt and this machine's own copy taken just before the switch.
    assert record['source_backup'] == 'kit.ccbackup'
    assert record['preservation']['name'] == preservation['name']
    assert base64.b64encode(machine.source_key) not in journal
    assert machine.source_key.hex().encode() not in journal
    settings, store = machine.opened()
    try:
        assert settings['generation'] == generation
        assert store.root == Path(selection['store'])
        assert store.read('manifest')['data']['project_id'] == machine.source_manifest['project_id']
        names = [item['data']['id'] for item in store.records('memories')[0]]
        assert names == ['source-memory']
        assert store.read('memories', 'source-memory')['data']['content'] == '来源机器的旧记忆（续写）'
        owner = store.state(OWNER)
        assert owner['installation'] == machine.installation and owner['windows_sid'] == WINDOWS_SID
        assert store.state(LOCK) == {'enabled': False, 'revision': 8, 'attempts': 0, 'until': 0}
        assert store.state(PREFERENCES)['revision'] == 'local-preference'
        assert store.state('desktop-siwc-validation') == LOCAL_SIWC
        assert store.state('desktop-qq-account') == LOCAL_QQ
        assert store.state(stable_key('desktop-wechat-selection', machine.installation)) == machine.wechat_selection
        assert store.state(desktop_wechat.SETUP) is None
        assert store.state(SESSION)['application'] == SESSION_ID
        assert sorted(store.state(HOLD)['platforms']) == sorted(DESKTOP_PLATFORMS)
        bound = desktop_identity.verify(store, installation=machine.installation,
                                        owner=ActorRef(**owner['actor']))
        assert bound['room'] == machine.source_owner
        assert bound['logical_role'] == machine.source_role
        assert bound['project_id'] == machine.source_manifest['project_id']
        assert store.state(desktop_identity.ROLE) == machine.source_role.as_dict()
        for key in RUN_KEYS:
            assert store.state(key, {}).get('status') != 'active'
        assert store.state('core-run:local')['reason'] == 'restore'
        receipt = store.state('desktop-last-restore')
        # The receipt names the source that was restored and, separately, this machine's own
        # copy taken just before the switch; the two files are different.
        assert receipt['status'] == 'complete' and receipt['source'] == 'portable'
        assert receipt['source_name'] == receipt['name'] == 'kit.ccbackup'
        assert receipt['preservation_name'] == preservation['name']
        assert receipt['preservation_name'] != receipt['source_name']
        with store.db() as db:
            assert db.execute('SELECT COUNT(*) FROM content_history').fetchone()[0] == 1
    finally:
        store.database.close()
    # The runtime login can read the new generation and still cannot create or migrate.
    runtime = {'host': '127.0.0.1', 'port': state['port'], 'user': 'qiban_runtime',
               'password': credential['database_password'], 'dbname': selection['database']}
    with psycopg.connect(**runtime) as db:
        assert db.execute('SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles '
                          'WHERE rolname=current_user').fetchone() == (False, False, False)
        assert db.execute("SELECT has_table_privilege(current_user,'content_records',"
                          "'SELECT,INSERT,UPDATE,DELETE')").fetchone()[0]
        assert not db.execute("SELECT has_table_privilege(current_user,'schema_migrations','INSERT')").fetchone()[0]
        assert not db.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0]
    # The staged credential slot holds the source key and this cluster's runtime password.
    slot = json.loads(machine.staging.library.get_password(storage_worker.VAULT, generation))
    assert set(slot) == {'data_key', 'database_password'}
    assert base64.b64decode(slot['data_key']) == machine.source_key
    assert slot['database_password'] == credential['database_password']


def test_a_stage_without_a_source_name_keeps_the_preservation_copy_separate(machine):
    """The host may hand over the authenticated pair only; no source name is invented.

    The desktop service hands the stager the blob and key it verified and passes no display
    name, so the receipt must not present this machine's own pre-restore copy as the source of
    the restore: that copy stays named only as a preservation copy.
    """
    result, preservation, _ = machine.staged()
    assert result['switched'] is True and result['restored'] is True
    _, store = machine.opened()
    try:
        receipt = store.state('desktop-last-restore')
        assert receipt['status'] == 'complete' and receipt['source'] == 'portable'
        # No source name at all: neither field carries the preservation copy's name.
        assert receipt['source_name'] is None and 'name' not in receipt
        assert receipt['preservation_name'] == preservation['name']
    finally:
        store.database.close()
    # The named preservation copy is the artifact the preview authenticated, and the journal
    # records the same two names: no source, and this machine's own copy.
    artifact = machine.root/'store'/'history'/PRESERVATION/receipt['preservation_name']
    assert artifact.is_file()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == preservation['sha256']
    journal = json.loads((machine.root/'generations'/result['generation']/'restore-journal.json')
                         .read_text(encoding='utf8'))
    assert journal['phase'] == 'committed' and journal['source_backup'] is None
    assert journal['preservation']['name'] == preservation['name']


def test_a_source_binding_is_adopted_through_its_own_physical_owner(machine):
    earlier_room = ActorRef('local', uuid.uuid4().hex, uuid.uuid4().hex)
    desktop_identity.bind(machine.source, installation=machine.source_installation,
                          owner=machine.source_owner, room=earlier_room,
                          logical_role=machine.source_role,
                          source={'installation': uuid.uuid4().hex, 'at': now()})
    result, _, _ = machine.staged()
    settings, store = machine.opened()
    try:
        assert settings['generation'] == result['generation']
        owner = store.state(OWNER)
        bound = desktop_identity.verify(store, installation=machine.installation,
                                        owner=ActorRef(**owner['actor']))
        assert bound['room'] == earlier_room
        assert bound['logical_role'] == machine.source_role
        assert bound['project_id'] == machine.source_manifest['project_id']
    finally:
        store.database.close()


def test_a_restore_hold_keeps_autorun_platforms_stopped_until_an_explicit_start(machine):
    result, _, _ = machine.staged()
    settings, store = machine.opened()
    try:
        assert settings['generation'] == result['generation']
        management = DesktopManagement(store, machine.installation, WINDOWS_SID)
        runtime = DesktopRuntime(management, 'portable-restart-session', model=object(),
                                 adapters=[Adapter()])
        rows = {row['platform']: row for row in runtime.status()['platforms']}
        assert store.state(SESSION)['application'] == 'portable-restart-session'
        assert rows['local']['held'] is True and rows['local']['autorun'] is True
        assert runtime.start('local', automatic=True) == {'started': False, 'reason': 'explicit_stop'}
        assert 'local' in store.state(HOLD)['platforms']
        assert runtime.start('local')['started'] is True
        assert 'local' not in store.state(HOLD)['platforms']
        assert 'wechat' in store.state(HOLD)['platforms']
        runtime.close()
        # A later application session still holds every platform the user did not start.
        again = DesktopRuntime(DesktopManagement(store, machine.installation, WINDOWS_SID),
                               'portable-restart-again', model=object(), adapters=[Adapter()])
        later = {row['platform']: row for row in again.status()['platforms']}
        assert later['local']['held'] is False
        assert later['wechat']['held'] is True
        again.close()
    finally:
        store.database.close()


@pytest.mark.parametrize('phase', ['source-verified', 'restored', 'validated', 'prepared'])
def test_an_interruption_before_the_commit_point_selects_nothing_new(machine, phase):
    def checkpoint(name):
        if name == phase:
            raise RuntimeError('synthetic interruption at ' + name)

    with pytest.raises(RuntimeError):
        machine.staged(checkpoint=checkpoint)
    selection = installation_generation.load_selection(machine.root, machine.installation)
    assert selection['generation'] is None and selection['database'] == 'qiban'
    assert not (machine.root/'active-generation.json').exists()
    # A staged attempt keeps its directory, database and journal as evidence.
    staged = sorted(generations(machine.root) - machine.staging.before_generations)
    if phase == 'source-verified':
        assert staged == []
    else:
        assert len(staged) == 1
        journal = json.loads((machine.root/'generations'/staged[0]/'restore-journal.json')
                             .read_text(encoding='utf8'))
        assert journal['phase'] == 'failed' and journal['generation'] == staged[0]


def test_an_interruption_after_the_commit_point_reports_the_switch(machine):
    def checkpoint(name):
        if name == 'committed':
            raise RuntimeError('synthetic interruption after the commit point')

    with pytest.raises(AssistantError) as error:
        machine.staged(checkpoint=checkpoint)
    assert error.value.code == 'portable_switch_applied'
    selection = installation_generation.load_selection(machine.root, machine.installation)
    assert selection['generation'] is not None
    assert selection['database'] == 'qiban_restore_' + selection['generation']


def test_current_data_changed_after_the_preview_is_refused(machine):
    blob = machine.exported()
    preservation, fingerprint = machine.preserved()
    machine.current.set_state('desktop-notice:after-preview', {'code': 'synthetic'})
    with pytest.raises(AssistantError) as error:
        portable_restore.stage_restore(current_store=machine.current, root=machine.root,
                                       source_blob=blob, source_key=machine.source_key,
                                       expected_current_fingerprint=fingerprint,
                                       preservation=preservation, application_session=SESSION_ID)
    assert error.value.code == 'portable_changed'
    assert installation_generation.load_selection(machine.root, machine.installation)['generation'] is None
    assert not [name for name in databases(machine.root) - machine.staging.before_databases
                if name.startswith('qiban_restore_')]


def test_a_store_from_another_database_is_refused(machine, tmp_path):
    other = PostgresStore(Path(tmp_path)/'other', machine.connect(machine.source_database),
                          machine.current_key, profile=machine.profile)
    try:
        blob = machine.exported()
        preservation, fingerprint = machine.preserved()
        with pytest.raises(AssistantError) as error:
            portable_restore.stage_restore(current_store=other, root=machine.root,
                                           source_blob=blob, source_key=machine.source_key,
                                           expected_current_fingerprint=fingerprint,
                                           preservation=preservation,
                                           application_session=SESSION_ID)
        assert error.value.code == 'portable_current'
    finally:
        other.database.close()


# -- this machine's own retained copies (source_mode='preserved') --------------------------

def retained(machine, name):
    """One artifact a preview wrote into the original generation's store."""
    return machine.root/'store'/'history'/PRESERVATION/name


def test_a_retained_copy_restores_this_machines_own_identity_into_a_fresh_generation(machine):
    first, artifact, _ = machine.staged()
    original_role = machine.current.state(desktop_identity.ROLE)
    _, bridge = machine.opened()
    try:
        blob = retained(machine, artifact['name']).read_bytes()
        report, fresh, _ = machine.staged(store=bridge, blob=blob, key=machine.current_key,
                                          source_name=artifact['name'], source_mode='preserved')
    finally:
        bridge.database.close()
    assert report['switched'] is True and report['restored'] is True
    assert report['generation'] != first['generation']
    selection = installation_generation.load_selection(machine.root, machine.installation)
    assert selection['generation'] == report['generation']
    # Two earlier generations, the original store and every retained copy survive.
    assert (machine.root/'generations'/first['generation']).is_dir()
    assert retained(machine, artifact['name']).is_file()
    settings, restored = machine.opened()
    try:
        assert settings['generation'] == report['generation']
        owner = restored.state(OWNER)
        assert owner['installation'] == machine.installation and owner['windows_sid'] == WINDOWS_SID
        # The original generation was never bound, and it comes back unbound with its own role.
        assert desktop_identity.binding(restored) is None
        assert restored.state(desktop_identity.ROLE) == original_role
        assert desktop_identity.resolve(restored, installation=machine.installation,
                                        owner=ActorRef(**owner['actor'])) is None
        # The bridged generation's own local state is what the staging preserved.
        assert restored.state(LOCK) == {'enabled': False, 'revision': 9, 'attempts': 0, 'until': 0}
        assert restored.state(PREFERENCES)['revision'] == 'local-preference'
        assert restored.state('desktop-siwc-validation') == LOCAL_SIWC
        assert restored.state('desktop-qq-account') == LOCAL_QQ
        assert restored.state(stable_key('desktop-wechat-selection', machine.installation)) \
            == machine.wechat_selection
        assert restored.state(desktop_wechat.SETUP) is None
        assert restored.state(SESSION)['application'] == SESSION_ID
        assert sorted(restored.state(HOLD)['platforms']) == sorted(DESKTOP_PLATFORMS)
        # The portable source's data is gone; this machine's own records are back.
        assert restored.records('memories')[0] == []
        receipt = restored.state('desktop-last-restore')
        assert receipt['status'] == 'complete' and receipt['source'] == 'preserved'
        # The retained copy that was restored and the second copy preserved for it are two
        # different files, and the receipt keeps both names for what they are.
        assert receipt['source_name'] == receipt['name'] == artifact['name']
        assert receipt['preservation_name'] == fresh['name']
        assert receipt['preservation_name'] != artifact['name']
        for key in RUN_KEYS:
            assert restored.state(key, {}).get('status') != 'active'
    finally:
        restored.database.close()
    journal = json.loads((machine.root/'generations'/report['generation']/'restore-journal.json')
                         .read_text(encoding='utf8'))
    assert journal['phase'] == 'committed' and journal['source_mode'] == 'preserved'
    assert journal['source_backup'] == artifact['name']
    assert journal['preservation']['name'] == fresh['name']
    assert machine.source_key.hex().encode() not in json.dumps(journal).encode()


def test_a_retained_copy_stage_without_a_source_name_names_only_the_preservation_copy(machine):
    """A preserved restore whose host names no source leaves the source unrecorded.

    The retained copy that was restored has a known file name of its own and the staged copy
    of the bridged generation is a different file; only a name the host supplies may be
    reported as the source, so the receipt records the preservation copy alone.
    """
    first, artifact, _ = machine.staged()
    _, bridge = machine.opened()
    try:
        blob = retained(machine, artifact['name']).read_bytes()
        report, fresh, _ = machine.staged(store=bridge, blob=blob, key=machine.current_key,
                                          source_mode='preserved')
    finally:
        bridge.database.close()
    assert report['switched'] is True and report['generation'] != first['generation']
    settings, restored = machine.opened()
    try:
        assert settings['generation'] == report['generation']
        # The retained copy of the original store was restored: no source memory is carried
        # back, and the original generation's unbound logical identity returns with it.
        assert restored.records('memories')[0] == []
        assert desktop_identity.binding(restored) is None
        receipt = restored.state('desktop-last-restore')
        assert receipt['status'] == 'complete' and receipt['source'] == 'preserved'
        assert receipt['source_name'] is None and 'name' not in receipt
        assert receipt['preservation_name'] == fresh['name']
        # The copy that was actually restored is another file, and it is still not named.
        assert receipt['preservation_name'] != artifact['name']
    finally:
        restored.database.close()
    journal = json.loads((machine.root/'generations'/report['generation']/'restore-journal.json')
                         .read_text(encoding='utf8'))
    assert journal['phase'] == 'committed' and journal['source_mode'] == 'preserved'
    assert journal['source_backup'] is None
    assert journal['preservation']['name'] == fresh['name']


def test_a_retained_copy_of_the_current_generation_keeps_its_bound_identity(machine):
    earlier_room = ActorRef('local', uuid.uuid4().hex, uuid.uuid4().hex)
    desktop_identity.bind(machine.source, installation=machine.source_installation,
                          owner=machine.source_owner, room=earlier_room,
                          logical_role=machine.source_role,
                          source={'installation': uuid.uuid4().hex, 'at': now()})
    first, _, _ = machine.staged()
    _, bridge = machine.opened()
    try:
        # Retain a copy of the generation that is current right now, then restore that copy.
        preservation, fingerprint = machine.preserved(store=bridge, key=machine.source_key)
        artifact = bridge.root/'history'/PRESERVATION/preservation['name']
        report, _, _ = machine.staged(store=bridge, blob=artifact.read_bytes(),
                                      key=machine.source_key, source_name=preservation['name'],
                                      preservation=preservation, fingerprint=fingerprint,
                                      source_mode='preserved')
    finally:
        bridge.database.close()
    assert report['generation'] != first['generation']
    settings, restored = machine.opened()
    try:
        assert settings['generation'] == report['generation']
        owner = restored.state(OWNER)
        bound = desktop_identity.verify(restored, installation=machine.installation,
                                        owner=ActorRef(**owner['actor']))
        assert bound is not None
        assert bound['room'] == earlier_room
        assert bound['logical_role'] == machine.source_role
        assert bound['project_id'] == machine.source_manifest['project_id']
        assert restored.state(desktop_identity.ROLE) == machine.source_role.as_dict()
        assert restored.read('memories', 'source-memory')['data']['content'] == '来源机器的旧记忆（续写）'
        assert restored.state(LOCK) == {'enabled': False, 'revision': 9, 'attempts': 0, 'until': 0}
        # Here the restored copy and this switch's preservation copy are one and the same
        # file, so both receipt fields name that single file.
        receipt = restored.state('desktop-last-restore')
        assert receipt['source_name'] == receipt['name'] == preservation['name']
        assert receipt['preservation_name'] == preservation['name']
    finally:
        restored.database.close()


def test_an_interruption_while_restoring_a_retained_copy_selects_nothing_new(machine):
    first, artifact, _ = machine.staged()
    _, bridge = machine.opened()
    try:
        blob = retained(machine, artifact['name']).read_bytes()
        preservation, fingerprint = machine.preserved(store=bridge, key=machine.source_key)

        def checkpoint(name):
            if name == 'prepared':
                raise RuntimeError('synthetic interruption at ' + name)

        with pytest.raises(RuntimeError):
            machine.staged(store=bridge, blob=blob, key=machine.current_key,
                           preservation=preservation, fingerprint=fingerprint,
                           checkpoint=checkpoint, source_mode='preserved')
        assert installation_generation.load_selection(
            machine.root, machine.installation)['generation'] == first['generation']
        staged = sorted(generations(machine.root) - machine.staging.before_generations
                        - {first['generation']})
        assert len(staged) == 1
        journal = json.loads((machine.root/'generations'/staged[0]/'restore-journal.json')
                             .read_text(encoding='utf8'))
        assert journal['phase'] == 'failed' and journal['source_mode'] == 'preserved'
    finally:
        bridge.database.close()


def test_current_data_changed_after_a_retained_copy_was_armed_is_refused(machine):
    first, artifact, _ = machine.staged()
    _, bridge = machine.opened()
    try:
        before_attempt = databases(machine.root)
        blob = retained(machine, artifact['name']).read_bytes()
        preservation, fingerprint = machine.preserved(store=bridge, key=machine.source_key)
        bridge.set_state('desktop-notice:after-preserved-preview', {'code': 'synthetic'})
        with pytest.raises(AssistantError) as error:
            machine.staged(store=bridge, blob=blob, key=machine.current_key,
                           preservation=preservation, fingerprint=fingerprint,
                           source_mode='preserved')
        assert error.value.code == 'portable_changed'
        assert installation_generation.load_selection(
            machine.root, machine.installation)['generation'] == first['generation']
        assert databases(machine.root) == before_attempt
    finally:
        bridge.database.close()


def test_an_unknown_source_mode_is_refused_before_anything_is_read(machine):
    with pytest.raises(AssistantError) as error:
        portable_restore.stage_restore(current_store=machine.current, root=machine.root,
                                       source_blob=b'CCBACKUP1:', source_key=b'k'*32,
                                       expected_current_fingerprint='0'*64,
                                       preservation={'name': 'x.ccbackup', 'sha256': '0'*64,
                                                     'bytes': 1},
                                       application_session=SESSION_ID, source_mode='page')
    assert error.value.code == 'portable_input'
