"""Isolated source-level integration of the upgrade transaction against a real PostgreSQL.

This is not a packaged build and not a frozen-payload acceptance: it drives the *source* modules
(``upgrade/``, ``installation``, ``portable_restore``) against a real PostgreSQL server started
from the published 0.9.4.175 binaries, with synthetic program trees that carry a small published
manifest instead of the 1.3 GB payload. Everything it touches lives below its own fresh evidence
directory; it never reads another profile, never prints a password or a key, and deletes only the
credential slots it generated itself.

What one batch covers:

    init            fresh installation root, synthetic Chinese/emoji content, seeded local lock
    prepare         preservation + clone through the existing restore path; the selection really
                    changes, the old generation's rows and ciphertext are untouched, and the
                    clone's restore journal names ``source_mode=preserved``
    gate/confirm    the new program initializes on the clone; ordinary business opening is
                    refused until the target program confirms, and afterwards the current and
                    previous generations are both registered and protected
    rollback        the new program preserves the current data and seals this machine's
                    whitelist; the previous program opens *only* the fallback, applies the
                    existing recovery rules, switches back and restores its published files
    cleanup         two consecutive successful upgrades make an older generation a candidate;
                    D61 cleanup frees its database, keeps the preservation copy listable and
                    decryptable, leaves the current and previous databases readable, refuses to
                    delete on a damaged receipt, and retries after the repair
    resume          an injected failure right after the pointer commit leaves a ``staging``
                    record; a restart of the same preparation resumes it instead of cloning again
    cross-schema    (batch D, selected with ``--batch d``) a controlled schema fixture: the
                    prepared clone is migrated from the checked-in migration set *plus* one
                    isolated 999 test migration that creates ``upgrade_probe``; the newer program
                    initializes the clone, writes a synthetic marker into that table, confirms, and
                    preserves the way back with a profile that knows the extra table. The previous
                    schema then refuses the clone with ``storage_schema_upgrade_required``, the
                    carrier rolls back to the old generation only, and the newer version's own
                    preservation copy still carries the controlled table and its marker.
    emergency       (batch E, selected with ``--batch e``) the same controlled fixture, but the
                    newer build's maintenance backend never runs: after the newer program has
                    written a row its predecessor cannot read, the recorded carrier seals the
                    stopped installation offline - cluster, stores, generations, pointer, settings,
                    credentials - and only then switches back. The sealed archive is re-read by a
                    temporary server on a copy of the archived cluster, which must still hold the
                    newer schema's row while the fallback holds neither; the sealed credential blob
                    is decrypted with the archive's own binding and compared with the live slot
                    without printing either; a repeat of either verb reuses the finished work, and
                    the finished emergency transaction is never cleaned automatically.
    retry           (batch F, selected with ``--batch f``) one preparation interrupted exactly where
                    a clone exists and the pointer has not moved: the failure is raised from the
                    ``before_commit`` hook, so the record stays ``staging`` and the old generation
                    stays selected. Preparing the same target again afterwards must drop that
                    half-finished clone (its database, its directory and its allocation entry)
                    without any separate ``abort``, rebuild, and reach ``staged``; a further retry
                    must keep that transaction and clone and write nothing; and the old
                    generation's roles and ciphertext must be exactly what the seed wrote. No
                    Windows prompt and no business model are involved.

Windows verification for the rollback steps is a clearly marked **test stub** installed through
``upgrade.verification.install``; no production receipt is written and no Windows prompt is
claimed.

Batches D and E are controlled schema fixtures, never a two-frozen-EXE acceptance: both programs
are the same small synthetic source trees the other batches build, the extra migration exists only
inside that batch's own evidence directory, and ``companion_core.postgres.__file__`` plus the
profile providers of ``installation``, ``upgrade.data`` and ``upgrade.service`` are replaced in
this test process only and restored in a ``finally``. No product SQL file, profile, service rule,
installer or business rule is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEV = REPO/'.dev'/'refactor-r0'/'desktop'/'upgrade-177'
FIXTURE_BIN = REPO/'.dev'/'refactor-r0'/'desktop'/'manual-app-175-siwc'/'resources'/'postgres'/'bin'
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(REPO/'src'))

import install_windows  # noqa: E402
import installation  # noqa: E402
import psycopg  # noqa: E402
import upgrade.data as data  # noqa: E402
import upgrade.emergency as emergency  # noqa: E402
import upgrade.journal as journal  # noqa: E402
import upgrade.program as program  # noqa: E402
import upgrade.registry as registry  # noqa: E402
import upgrade.service as service  # noqa: E402
import upgrade.verification as verification  # noqa: E402
from credential_vault import VAULT, VaultCredentials  # noqa: E402

from companion_core import postgres as postgres_module  # noqa: E402
from companion_core import postgres_transfer  # noqa: E402
from companion_core.errors import AssistantError  # noqa: E402
from companion_core.postgres import PostgresStore, verify_schema  # noqa: E402
from companion_core.process_scope import ProcessScope  # noqa: E402
from qiban import desktop_management  # noqa: E402
from qiban.desktop_profile import desktop_profile  # noqa: E402
from qiban.desktop_runtime import HOLD as HOLD_KEY  # noqa: E402
from qiban.desktop_runtime import SESSION as SESSION_KEY  # noqa: E402

IMMEDIATE = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
OWNER = desktop_management.LOCK
SEED_TEXT = '栖伴源码集成验证 🐉 表情与中文都必须原样保留'
OLD_FILES = {'Qiban.exe': b'MZ old shell\n', 'README.txt': b'old notes\n',
             'resources/storage/worker.txt': b'old worker\n'}
NEW_FILES = {'Qiban.exe': b'MZ new shell\n', 'README.txt': b'new notes\n',
             'resources/storage/worker.txt': b'new worker\n',
             'tools/new-only.txt': b'published only by the newer build\n'}
# Batch D is a controlled schema fixture. The extra migration and its table are written only into
# that batch's own evidence directory, never into the checked-in migration set.
CONTROLLED_VERSION = 999
CONTROLLED_SCRIPT = f'{CONTROLLED_VERSION}_upgrade_probe.sql'
CONTROLLED_TABLE = 'upgrade_probe'
CONTROLLED_MIGRATION = 'CREATE TABLE upgrade_probe(id TEXT PRIMARY KEY, body TEXT NOT NULL);\n'
PROBE_MARKER = {'id': 'source-integration-marker',
                'body': 'upgrade_probe 受控迁移标记 🐉 新版本表格必须保全这一行'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def build_app(directory, files, name):
    """A synthetic published program tree: the files plus their own identity document."""
    rows = []
    for path, content in sorted(files.items()):
        target = Path(directory)/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        rows.append({'path': path, 'bytes': len(content), 'sha256': digest(content)})
    identity = {'format': 1, 'count': len(rows), 'bytes': sum(row['bytes'] for row in rows),
                'files': rows}
    (Path(directory)/program.IDENTITY).write_bytes(
        (json.dumps(identity, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf8'))
    return program.document(directory)


def install_over(installed, package, published):
    """What the installer does: overwrite the published file set, keeping nothing stale."""
    installed, package = Path(installed), Path(package)
    wanted = {row['path'] for row in published['files']}
    for path in sorted(wanted):
        target = installed/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((package/path).read_bytes())
    for path in sorted(installed.rglob('*')):
        relative = path.relative_to(installed).as_posix()
        if path.is_file() and relative != program.IDENTITY and relative not in wanted:
            path.unlink()
    (installed/program.IDENTITY).write_bytes((package/program.IDENTITY).read_bytes())
    return program.document(installed)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Server:
    """One PostgreSQL server this driver started, and therefore owns."""

    def __init__(self, root, binaries, port):
        self.root, self.binaries, self.port = Path(root), Path(binaries), port
        self.process = None
        self.scope = ProcessScope()

    def start(self, timeout=40):
        logs = self.root/'logs'
        logs.mkdir(parents=True, exist_ok=True)
        log = (logs/'source-integration-server.log').open('ab')
        self.process = installation.owned(
            [str(self.binaries/'postgres.exe'), '-D', str(self.root/'database'),
             '-p', str(self.port)], self.scope, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        log.close()
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('source_integration_server_exited')
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', self.port)) == 0:
                    return self
            time.sleep(.1)
        raise RuntimeError('source_integration_server_timeout')

    def stop(self):
        if self.process is None:
            return
        try:
            subprocess.run([str(self.binaries/'pg_ctl.exe'), '-D', str(self.root/'database'),
                            '-m', 'fast', '-w', '-t', '20', 'stop'], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25,
                           creationflags=IMMEDIATE)
        finally:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
            self.scope.close()
            self.process = None


class Run:
    """One batch: steps, evidence and the exact errors that happened."""

    def __init__(self, evidence):
        self.evidence = Path(evidence)
        self.steps = []
        self.notes = []
        self.slots = []
        self.servers = []

    def step(self, name, function):
        started = time.monotonic()
        try:
            detail = function()
        except Exception as error:  # noqa: BLE001 - the batch records what failed
            code = getattr(error, 'code', type(error).__name__)
            self.steps.append({'step': name, 'ok': False, 'error': str(code),
                               'detail': traceback.format_exc(limit=3).splitlines()[-6:],
                               'seconds': round(time.monotonic()-started, 3)})
            raise
        self.steps.append({'step': name, 'ok': True, 'detail': detail,
                           'seconds': round(time.monotonic()-started, 3)})
        return detail

    def note(self, text):
        self.notes.append(text)

    def server(self, root, binaries, port):
        value = Server(root, binaries, port)
        self.servers.append(value)
        return value.start()

    def close(self):
        for server in reversed(self.servers):
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass


def shim_acl(run, probe_root):
    """Exercise actual Windows ACL setup; a failure fails this batch."""
    install_windows.protect_directory(probe_root)
    return {'attempted': True, 'denied': False}


def seed(root, installation_id, port):
    """Synthetic Chinese/emoji content plus this machine's local lock state."""
    credential = data.credentials(VaultCredentials(), installation_id)
    store_root = data.store_root(root, 'store')
    options = {'host': '127.0.0.1', 'port': port, 'dbname': 'qiban', 'user': 'qiban_setup',
               'password': credential['bootstrap_password'], 'connect_timeout': 5}
    store = PostgresStore(store_root, options, credential['data_key'], profile=desktop_profile())
    try:
        version = store.version('persona')
        persona = store.read('persona')['data']
        persona['name'] = '源码集成角色'
        persona['interests'] = [SEED_TEXT, '升级后必须原样保留']
        store.write('persona', persona, version, 'source-integration')
        memory = {'schema_version': 1, 'id': 'source-integration-memory',
                  'kind': 'user_preference', 'content': SEED_TEXT,
                  'source': {'type': 'user_instruction', 'recorded_at':
                             '2026-11-11T00:00:00+00:00', 'reference': 'source-integration:seed'},
                  'confidence': 'explicit', 'tags': ['源码集成'], 'locked': False}
        store.write('memories', memory, None, 'source-integration', item_id='source-integration-memory')
        salt = '15'*16
        store.set_state(OWNER, {'enabled': True, 'revision': 3, 'salt': salt,
                               'hash': desktop_management.password_hash('isolated-fixture-pass', salt),
                               'attempts': 0, 'until': 0})
        ciphertext = {}
        with store.db() as db:
            for kind, item in (('persona', '@config'), ('memories', 'source-integration-memory')):
                row = db.execute('SELECT body FROM content_records WHERE kind=? AND id=?',
                                 (kind, item)).fetchone()
                ciphertext[f'{kind}:{item}'] = digest(bytes(row[0]))
        return {'persona': persona['name'], 'ciphertext': ciphertext,
                'lock': {k: store.state(OWNER)[k] for k in ('enabled', 'revision')}}
    finally:
        store.database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, default=DEV/'source-integration-01')
    parser.add_argument('--binaries', type=Path, default=FIXTURE_BIN)
    parser.add_argument('--batch', dest='batches', action='append',
                        choices=('a', 'b', 'c', 'd', 'e', 'f'),
                        help='run only the named batch (repeatable); the default runs all of them')
    args = parser.parse_args(argv)
    evidence = args.evidence.resolve()
    if evidence.exists():
        print(json.dumps({'error': 'evidence_exists', 'path': str(evidence)}))
        return 2
    if not (args.binaries/'postgres.exe').is_file():
        print(json.dumps({'error': 'postgres_binaries_missing', 'path': str(args.binaries)}))
        return 2
    evidence.mkdir(parents=True)
    run = Run(evidence)
    report = {'passed': False, 'binaries': str(args.binaries), 'steps': run.steps,
              'notes': run.notes, 'credentials': {'created': 0, 'removed': None},
              'windows_verification': 'test stub only', 'frozen_payload': False,
              'fixture': {'programs': 'synthetic source program trees',
                          'frozen_executables': False,
                          'schema': 'controlled: isolated migration copy plus one 999 test migration',
                          'source_sql_unchanged': True}}
    data.BINARIES = str(args.binaries)
    vault = VaultCredentials()
    slots = []
    try:
        probe = evidence/'acl-probe'
        probe.mkdir()
        report['acl'] = shim_acl(run, probe)
        worker = evidence/'worker-placeholder.exe'
        worker.write_bytes(b'placeholder worker path\n')

        batches = (('a', 'batch_a_installation', batch_a),
                   ('b', 'batch_b_two_upgrades', batch_b),
                   ('c', 'batch_c_resume', batch_c),
                   ('d', 'batch_d_cross_schema', batch_d),
                   ('e', 'batch_e_emergency', batch_e),
                   ('f', 'batch_f_prepare_retry', batch_f))
        report['batches'] = [key for key, _, _ in batches
                             if not args.batches or key in args.batches]
        for key, name, function in batches:
            if args.batches and key not in args.batches:
                continue
            try:
                run.step(name, lambda function=function: function(
                    run, evidence, args, vault, slots, worker, args.binaries))
            except Exception:
                run.close()
        report['passed'] = all(step['ok'] for step in run.steps)
    except Exception:  # noqa: BLE001
        report['failed_step'] = run.steps[-1]['step'] if run.steps else 'unknown'
    finally:
        run.close()
        report['steps'] = run.steps
        report['notes'] = run.notes
        # Capture slots even when staging raised before returning its new generation.
        for manifest in evidence.glob('batch-*/Profile/generations/*/restore-journal.json'):
            if evidence not in manifest.resolve().parents:
                continue
            value = json.loads(manifest.read_text(encoding='utf8'))
            if value.get('installation') in slots and journal.IDENTIFIER.fullmatch(manifest.parent.name):
                slots.append(manifest.parent.name)
        slots[:] = list(dict.fromkeys(slots))
        report['credentials']['created'] = len(slots)
        removed, leftover = [], []
        for slot in slots:
            try:
                if vault.get_password(VAULT, slot) is None:
                    removed.append(slot)
                    continue
                vault.delete_password(VAULT, slot)
                (removed if vault.get_password(VAULT, slot) is None else leftover).append(slot)
            except Exception:  # noqa: BLE001
                leftover.append(slot)
        report['credentials'].update({'removed': len(removed), 'slots': removed,
                                      'leftover': leftover})
        report['processes_left'] = [server.process.pid for server in run.servers
                                    if server.process is not None]
        (evidence/'result.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)+'\n', encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'steps': [
        {'step': step['step'], 'ok': step['ok'], 'error': step.get('error')}
        for step in report['steps']]}, ensure_ascii=False))
    return 0 if report['passed'] else 1


# -- batch A: installation, prepare, gate/confirm, rollback -------------------

def new_root(run, evidence, vault, slots, name, binaries, worker):
    root = evidence/name/'Profile'
    root.mkdir(parents=True)
    installation_initialize = installation.initialize
    try:
        installation_initialize(root, binaries, worker, None)
    finally:
        record = root/'installation.json'
        if record.is_file():
            identity = json.loads(record.read_text(encoding='utf8')).get('installation')
            if isinstance(identity, str) and journal.IDENTIFIER.fullmatch(identity):
                slots.append(identity)
    state = data.state(root)
    return root, state


def batch_a(run, evidence, args, vault, slots, worker, binaries):
    detail = {}
    root, state = new_root(run, evidence, vault, slots, 'batch-a', binaries, worker)
    installation_id = state['installation']
    port = state['port']
    detail['installation'] = installation_id
    server = run.server(root, binaries, port)
    detail['seed'] = run.step('seed', lambda: seed(root, installation_id, port))
    server.stop()
    old_app = evidence/'batch-a'/'app-old'
    new_app = evidence/'batch-a'/'app-new'
    installed = evidence/'batch-a'/'app-installed'
    old_published = build_app(old_app, OLD_FILES, 'old')
    new_published = build_app(new_app, NEW_FILES, 'new')
    installed.mkdir()
    install_over(installed, old_app, old_published)
    detail['identities'] = {'old': old_published['sha256'], 'new': new_published['sha256']}

    before = data.selected(root, installation_id)
    prepared = run.step('prepare', lambda: service.prepare(
        root, installed, target_identity=new_published['sha256']))
    after = data.selected(root, installation_id)
    record = journal.load(root)
    clone = record['clone']
    restore_journal = (root/'generations'/clone['generation']/'restore-journal.json')
    restore = json.loads(restore_journal.read_text(encoding='utf8'))
    detail['selection'] = {'before': journal.selection(before), 'after': journal.selection(after),
                           'prepared': prepared['generation']}
    detail['restore_journal'] = {'phase': restore.get('phase'),
                                 'source_mode': restore.get('source_mode'),
                                 'installation': restore.get('installation') == installation_id,
                                 'preservation': restore.get('preservation')}
    assert after['generation'] == clone['generation'] != before['generation']
    assert restore.get('source_mode') == 'preserved'
    server = run.server(root, binaries, port)
    detail['old_data'] = run.step('old_generation_untouched', lambda: old_generation_state(
        root, state, before, detail['seed']['ciphertext']))
    server.stop()

    installed_published = install_over(installed, new_app, new_published)
    assert installed_published['sha256'] == new_published['sha256']
    run.step('new_program_initializes_clone', lambda: installation.initialize(
        root, binaries, installed/'resources/storage/worker.txt', None))
    detail['gate_before_confirm'] = run.step('gate_before_confirm', lambda: gate_refusal(
        root))
    server = run.server(root, binaries, port)
    detail['confirm'] = run.step('confirm', lambda: service.confirm(
        root, installed))
    detail['gate_after_confirm'] = run.step('gate_after_confirm', lambda: business_opens(
        root))
    detail['registry_after_confirm'] = run.step('registry_after_confirm', lambda: (
        registered(registry.load(root, installation_id))))
    server.stop()

    verification.install(lambda request: {'session': 'source-integration-test-stub',
                                           'at': utcnow()})
    run.note('rollback verification channel: test stub, no production receipt, no Windows prompt')
    detail['rollback_prepare'] = run.step('rollback_prepare', lambda: service.rollback_prepare(
        root, app=installed))
    sealed = journal.load(root)['rollback']['settings']
    assert sealed is not None and SEED_TEXT not in json.dumps(sealed)
    carrier = program.tree(root, record['carrier']['identity_sha256'])
    original_executing = service.executing_app
    service.executing_app = lambda: carrier
    try:
        detail['rollback'] = run.step('rollback', lambda: service.rollback(
            root, installed_app=installed))
    finally:
        service.executing_app = original_executing
    after_rollback = data.selected(root, installation_id)
    detail['selection_after_rollback'] = journal.selection(after_rollback)
    assert after_rollback['generation'] == record['origin']['selection']['generation']
    server = run.server(root, binaries, port)
    detail['fallback_state'] = run.step('fallback_recovery_rules', lambda: fallback_state(
        root, state, after_rollback))
    server.stop()
    detail['reopen'] = run.step('reopen', lambda: reopen(root, installed, state, run,
                                                binaries, port))
    detail['program_after_rollback'] = run.step(
        'program_after_rollback', lambda: program_state(installed, old_published))
    run.note(f'batch A installation {installation_id} verified')
    return detail


def old_generation_state(root, state, selection, ciphertext):
    """The previous generation's rows and ciphertext must be exactly what they were."""
    credential = data.credentials(VaultCredentials(), state['installation'])
    key = data.slot_key(VaultCredentials(), state['installation'], selection['credential_id'])
    options = {'host': '127.0.0.1', 'port': state['port'], 'dbname': selection['database'],
               'user': 'qiban_setup', 'password': credential['bootstrap_password'],
               'connect_timeout': 5}
    store = PostgresStore(Path(selection['store']), options, key,
                          profile=desktop_profile())
    try:
        verify_schema(store.database)
        seen = {}
        with store.db() as db:
            for kind, item in (('persona', '@config'), ('memories', 'source-integration-memory')):
                row = db.execute('SELECT body FROM content_records WHERE kind=? AND id=?',
                                 (kind, item)).fetchone()
                seen[f'{kind}:{item}'] = digest(bytes(row[0]))
        assert seen == ciphertext, 'the old generation ciphertext changed'
        persona = store.read('persona')['data']
        assert persona['interests'][0] == SEED_TEXT
        fingerprint = postgres_transfer.fingerprint(store)
        return {'ciphertext_matches': True, 'fingerprint': fingerprint,
                'lock': {k: store.state(OWNER)[k] for k in ('enabled', 'revision')}}
    finally:
        store.database.close()


def gate_refusal(root):
    """Ordinary business opening must be refused while the upgrade waits for confirmation."""
    import storage_worker
    try:
        storage_worker.open_store(root/'storage-config.json')
    except Exception as error:  # noqa: BLE001
        code = getattr(error, 'code', type(error).__name__)
        assert code in ('upgrade_identity_unknown', 'upgrade_confirmation_required'), code
        return {'refused': True, 'code': code}
    raise AssertionError('business opening was allowed before confirm')


def business_opens(root):
    import storage_worker
    store = storage_worker.open_store(root/'storage-config.json')
    try:
        persona = store.read('persona')['data']
        return {'opened': True, 'persona': persona['name']}
    finally:
        store.database.close()


def registered(value):
    return {'generations': [{'generation': entry['generation'],
                             'receipt': entry['preservation'] is not None}
                            for entry in value['generations']],
            'programs': [{'identity': entry['identity_sha256'][:8], 'roles': entry['roles']}
                         for entry in value['programs']]}


def fallback_state(root, state, selection):
    """The restored generation keeps this machine's lock, holds every platform and is readable."""
    credential = data.credentials(VaultCredentials(), state['installation'])
    key = data.slot_key(VaultCredentials(), state['installation'], selection['credential_id'])
    options = {'host': '127.0.0.1', 'port': state['port'], 'dbname': selection['database'],
               'user': 'qiban_setup', 'password': credential['bootstrap_password'],
               'connect_timeout': 5}
    store = PostgresStore(Path(selection['store']), options, key,
                          profile=desktop_profile())
    try:
        verify_schema(store.database)
        lock = store.state(OWNER)
        hold = store.state(HOLD_KEY)
        session = store.state(SESSION_KEY)
        persona = store.read('persona')['data']
        assert lock['enabled'] is True and lock['revision'] == 5, 'the local lock did not survive'
        assert lock['hash'] == desktop_management.password_hash('isolated-fixture-pass', lock['salt'])
        assert hold and hold['platforms'], 'platforms were not held'
        assert session['held'] == hold['platforms'], 'the session does not hold every platform'
        return {'lock': {k: lock[k] for k in ('enabled', 'revision')}, 'held_platforms': hold['platforms'], 'persona': persona['name']}
    finally:
        store.database.close()


def reopen(root, installed, state, run, binaries, port):
    """The previous program opens its own generation again and business opening is allowed."""
    installation.initialize(root, Path(data.BINARIES), installed/'resources/storage/worker.txt', None)
    server = run.server(root, binaries, port)
    try:
        inside = business_opens(root)
    finally:
        server.stop()
    return {'ready': data.state(root)['phase'] == 'ready', 'business': inside}


def program_state(installed, published):
    current = program.document(installed)
    program.verify(installed, published)
    extra = (Path(installed)/'tools'/'new-only.txt').exists()
    return {'identity': current['sha256'], 'matches_previous': current['sha256'] == published['sha256'],
            'newer_extra_file_removed': not extra}


# -- batch B: two upgrades, D61 cleanup and a damaged receipt -----------------




def utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def batch_b(run, evidence, args, vault, slots, worker, binaries):
    detail = {}
    base = evidence/'batch-b'
    root, state = new_root(run, evidence, vault, slots, 'batch-b', binaries, worker)
    installation_id = state['installation']
    port = state['port']
    server = run.server(root, binaries, port)
    detail['seed'] = run.step('seed', lambda: seed(root, installation_id, port))
    server.stop()
    installed = base/'app-installed'
    installed.mkdir()
    builds = []
    for name, files in (('build-1', {'Qiban.exe': b'MZ build 1\n', 'README.txt': b'notes 1\n'}),
                        ('build-2', {'Qiban.exe': b'MZ build 2\n', 'README.txt': b'notes 2\n',
                                     'tools/new-only.txt': b'only build 2\n'})):
        files = {**files, 'resources/storage/worker.txt': name.encode('ascii')}
        directory = base/name
        directory.mkdir()
        builds.append((directory, build_app(directory, files, name)))
    old = base/'build-0'
    initial = build_app(old, OLD_FILES, 'initial')
    install_over(installed, old, initial)
    for index, (directory, published) in enumerate(builds):
        run.step(f'upgrade_{index+1}_prepare', lambda directory=directory, published=published:
                 service.prepare(root, installed, target_identity=published['sha256']))
        install_over(installed, directory, published)
        run.step(f'upgrade_{index+1}_initialize', lambda: installation.initialize(
            root, binaries, installed/'resources/storage/worker.txt', None))
        server = run.server(root, binaries, port)
        try:
            record = journal.load(root)
            if index == 1:
                detail['damaged_receipt'] = run.step(
                    'damaged_receipt_blocks_cleanup', lambda: damage_and_confirm(
                        run, root, installed, record, vault))
            else:
                run.step('upgrade_1_confirm', lambda: service.confirm(root, installed))
        finally:
            server.stop()
    detail['registry'] = run.step('registry_after_two_upgrades', lambda: (
        registered(registry.load(root, installation_id))))
    server = run.server(root, binaries, port)
    try:
        detail['retry'] = run.step('cleanup_retry_after_repair', lambda: cleanup_retry(
            root, vault))
        detail['provider'] = run.step('preservation_provider', lambda: provider_check(
            root, installation_id, vault))
        detail['readable'] = run.step('current_and_previous_readable', lambda: readable(
            root, state))
    finally:
        server.stop()
    run.note(f'batch B installation {installation_id} verified')
    return detail


def damage_and_confirm(run, root, installed, record, vault):
    """Corrupt the oldest candidate's own preservation, confirm, and prove nothing was dropped."""
    original = next(entry for entry in registry.load(root, record['installation'])['generations']
                    if entry['generation'] is None)
    artifact = data.store_root(root, original['store'])/'history'/'portable-preservation' \
        /original['preservation']['name']
    original_bytes = artifact.read_bytes()
    artifact.write_bytes(b'CCBACKUP1:'+b'9'*32+b'\n'+b'damaged')
    report = service.confirm(root, installed)
    dropped = [entry for entry in report['cleanup']['cleanup']['generations']]
    assert report['cleanup']['complete'] is False, 'a damaged receipt must not look complete'
    assert dropped == [], 'nothing may be dropped while its own receipt is damaged'
    artifact.write_bytes(original_bytes)
    return {'complete': report['cleanup']['complete'],
            'failed': [entry['code'] for entry in report['cleanup']['cleanup']['failed']]}


def cleanup_retry(root, vault):
    report = service.core_cleanup(root, vault=vault)
    assert report['complete'] is True, report['cleanup']['failed']
    return {'generations': [entry['generation'] for entry in report['cleanup']['generations']],
            'complete': report['complete']}


def provider_check(root, installation_id, vault):
    """The preserved copies must still be listable and decryptable after the cleanup."""
    from portable_preservation import preserved_provider

    provider = preserved_provider(
        root, installation_id, lambda keyref: data.slot_key(vault, installation_id, keyref))
    page = provider.page()
    listings = [{'keyref': item['handle'][:4], 'name': item['name'], 'usable': item['usable']}
                for item in page['items']]
    assert listings and all(item['usable'] for item in listings), 'a preserved copy is unusable'
    entry = provider.open(page['items'][0]['handle'])
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    backup_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                      info=b'companion-postgres-backup-v1').derive(entry['key'])
    snapshot = postgres_transfer.unpack(entry['blob'], backup_key)
    return {'copies': len(listings), 'snapshot_tables': len(snapshot['tables']),
            'first': listings[0]['name']}


def readable(root, state):
    """The current and the previous generation databases are both still usable."""
    installation_id = state['installation']
    current = data.selected(root, installation_id)
    record = journal.load(root, required=True)
    previous = record['origin']['selection']
    seen = []
    for selection in (current, previous):
        credential = data.credentials(VaultCredentials(), installation_id)
        key = data.slot_key(VaultCredentials(), installation_id, selection['credential_id'])
        options = {'host': '127.0.0.1', 'port': state['port'], 'dbname': selection['database'],
                   'user': 'qiban_setup', 'password': credential['bootstrap_password'],
                   'connect_timeout': 5}
        store = PostgresStore(data.store_root(root, journal.selection(selection)['store']),
                              options, key, profile=desktop_profile())
        try:
            verify_schema(store.database)
            persona = store.read('persona')['data']
            seen.append({'generation': selection['generation'],
                         'database': selection['database'], 'persona': persona['name']})
        finally:
            store.database.close()
    return seen


# -- batch C: pointer commit, then an interrupted record write ----------------

def batch_c(run, evidence, args, vault, slots, worker, binaries):
    base = evidence/'batch-c'
    root, state = new_root(run, evidence, vault, slots, 'batch-c', binaries, worker)
    installation_id = state['installation']
    port = state['port']
    server = run.server(root, binaries, port)
    run.step('seed', lambda: seed(root, installation_id, port))
    server.stop()
    old_app, new_app, installed = base/'app-old', base/'app-new', base/'app-installed'
    old_published = build_app(old_app, OLD_FILES, 'old')
    new_published = build_app(new_app, NEW_FILES, 'new')
    installed.mkdir()
    install_over(installed, old_app, old_published)
    real_advance = journal.advance
    crashed = {'done': False}

    def failing_advance(root_value, value, phase, **updates):
        if phase == 'staged' and not crashed['done']:
            crashed['done'] = True
            raise RuntimeError('source_integration_crash_after_pointer_commit')
        return real_advance(root_value, value, phase, **updates)

    journal.advance = failing_advance
    try:
        try:
            service.prepare(root, installed, target_identity=new_published['sha256'])
        except RuntimeError:
            pass
        else:
            raise AssertionError('the injected failure did not happen')
    finally:
        journal.advance = real_advance
    stranded = journal.load(root)
    pointer = data.selected(root, installation_id)
    assert stranded['phase'] == 'staging', stranded['phase']
    assert pointer['generation'] == stranded['plan']['generation'], 'the pointer did not move'
    status = service.status(root, installed)
    resumed = service.prepare(root, installed, target_identity=new_published['sha256'])
    recovered = journal.load(root)
    assert recovered['phase'] == 'staged' and resumed.get('resumed') is True
    install_over(installed, new_app, new_published)
    run.step('initialize_after_resume', lambda: installation.initialize(root, binaries, installed/'resources/storage/worker.txt', None))
    server = run.server(root, binaries, port)
    try:
        confirmed = run.step('confirm_after_resume', lambda: service.confirm(root, installed))
    finally:
        server.stop()
    return {'stranded_phase': stranded['phase'], 'status_reconcile': status['reconcile'],
            'resumed': resumed.get('resumed'), 'recovered_phase': recovered['phase'],
            'confirmed': confirmed['phase'],
            'selection': journal.selection(data.selected(root, installation_id))}


# -- batch D: controlled cross-schema upgrade, rollback and preservation ------

def controlled_schema_copy(base):
    """Copy the checked-in migration set into this batch and add one controlled migration.

    Nothing below ``src`` is written: the copy - including the extra migration that creates
    ``upgrade_probe`` - exists only inside this batch's own evidence directory, so the checked-in
    schema of the product stays exactly what it was.
    """
    source = Path(postgres_module.__file__)
    scripts = source.resolve().parent/'sql'
    names = sorted(path.name for path in scripts.glob('[0-9]*.sql'))
    versions = {int(name.split('_', 1)[0]) for name in names}
    assert names, 'the checked-in migration set is empty'
    assert CONTROLLED_VERSION not in versions, 'the checked-in migrations already use 999'
    folder = Path(base)/'controlled-schema'
    target = folder/'sql'
    target.mkdir(parents=True, exist_ok=False)
    for name in names:
        (target/name).write_bytes((scripts/name).read_bytes())
    (target/CONTROLLED_SCRIPT).write_bytes(CONTROLLED_MIGRATION.encode('ascii'))
    isolated = folder/source.name
    isolated.write_bytes(source.read_bytes())
    return {'folder': folder, 'file': isolated, 'scripts': len(names)+1,
            'version': CONTROLLED_VERSION, 'script': CONTROLLED_SCRIPT,
            'table': CONTROLLED_TABLE}


@contextmanager
def controlled_schema_context(module_file, profile):
    """Point the real migrate/verify at the isolated scripts and every store profile at the copy.

    ``companion_core.postgres.__file__`` is exactly what ``migrate`` and ``verify_schema`` read
    their ``sql`` folder from, so the product code itself applies the extra migration. The three
    modules that hand a profile to a store are replaced as well, so the installation bootstrap,
    the maintenance session and the rollback preparation all migrate, snapshot and preserve with
    the profile that knows the extra table. This test process only: every replacement is restored
    in ``finally``.
    """
    module_path = postgres_module.__file__
    providers = ((installation, installation.desktop_profile),
                 (data, data.desktop_profile),
                 (service, service.desktop_profile))

    def controlled():
        return profile

    try:
        postgres_module.__file__ = str(module_file)
        for module, _original in providers:
            module.desktop_profile = controlled
        yield {'file': Path(module_file).name, 'version': CONTROLLED_VERSION,
               'extra_tables': sorted(profile.extra_tables)}
    finally:
        postgres_module.__file__ = module_path
        for module, original in providers:
            module.desktop_profile = original


def old_schema_clone(root, installed, vault, selection):
    """The freshly prepared clone is still exactly the checked-in schema, without the extra table."""
    with data.session(root, installed, vault=vault, profile=desktop_profile(),
                      selection=selection) as (store, _key, _value, _credential, _descriptor):
        verify_schema(store.database)
        with store.db() as db:
            absent = db.execute("SELECT to_regclass('upgrade_probe')").fetchone()[0] is None
            versions = [int(row[0]) for row in
                        db.execute('SELECT version FROM schema_migrations ORDER BY version')]
        persona = store.read('persona')['data']
    assert absent, 'the prepared clone already has the controlled table'
    assert CONTROLLED_VERSION not in versions, versions
    return {'versions': versions, 'new_table_absent': True, 'persona': persona['name']}


def controlled_marker(root, vault, profile):
    """Write one synthetic marker row into the newer schema's table, through the real store."""
    with data.live(root, vault=vault, profile=profile) as (store, _key, _value, _credential,
                                                           descriptor):
        with store.db() as db:
            db.execute('INSERT INTO upgrade_probe(id, body) VALUES(?,?)',
                       (PROBE_MARKER['id'], PROBE_MARKER['body']))
            rows = [[row[0], row[1]] for row in
                    db.execute('SELECT id, body FROM upgrade_probe ORDER BY id')]
            versions = [int(row[0]) for row in
                        db.execute('SELECT version FROM schema_migrations ORDER BY version')]
        assert [PROBE_MARKER['id'], PROBE_MARKER['body']] in rows
        return {'generation': descriptor['generation'], 'table': CONTROLLED_TABLE,
                'versions': versions, 'rows': rows}


def old_schema_refuses(root, installed, vault, selection):
    """The previous program's schema check must refuse the clone the newer build migrated."""
    refusal = None
    with data.session(root, installed, vault=vault, profile=desktop_profile(),
                      selection=selection) as (store, _key, _value, _credential, _descriptor):
        with store.db() as db:
            versions = [int(row[0]) for row in
                        db.execute('SELECT version FROM schema_migrations ORDER BY version')]
        try:
            verify_schema(store.database)
        except AssistantError as error:
            refusal = getattr(error, 'code', type(error).__name__)
        else:
            raise AssertionError('the previous schema accepted the newer clone')
    assert refusal == 'storage_schema_upgrade_required', refusal
    assert CONTROLLED_VERSION in versions, versions
    return {'refused': True, 'code': refusal, 'versions': versions}


def controlled_fallback_state(root, state, selection, ciphertext):
    """The rolled-back generation: previous schema only, same ciphertext, same memory."""
    credential = data.credentials(VaultCredentials(), state['installation'])
    key = data.slot_key(VaultCredentials(), state['installation'], selection['credential_id'])
    options = {'host': '127.0.0.1', 'port': state['port'], 'dbname': selection['database'],
               'user': 'qiban_setup', 'password': credential['bootstrap_password'],
               'connect_timeout': 5}
    store = PostgresStore(Path(selection['store']), options, key, profile=desktop_profile())
    try:
        verify_schema(store.database)
        with store.db() as db:
            absent = db.execute("SELECT to_regclass('upgrade_probe')").fetchone()[0] is None
            versions = [int(row[0]) for row in
                        db.execute('SELECT version FROM schema_migrations ORDER BY version')]
            seen = {}
            for kind, item in (('persona', '@config'), ('memories', 'source-integration-memory')):
                row = db.execute('SELECT body FROM content_records WHERE kind=? AND id=?',
                                 (kind, item)).fetchone()
                seen[f'{kind}:{item}'] = digest(bytes(row[0]))
        persona = store.read('persona')['data']
        memory = store.read('memories', 'source-integration-memory')['data']
        lock = store.state(OWNER)
        assert absent, 'the fallback still has the controlled table'
        assert seen == ciphertext, 'the fallback ciphertext changed'
        assert persona['name'] == '源码集成角色' and persona['interests'][0] == SEED_TEXT
        assert memory['content'] == SEED_TEXT, 'the seeded memory is no longer readable'
        assert lock['enabled'] is True, 'the local lock did not survive the rollback'
        assert lock['hash'] == desktop_management.password_hash('isolated-fixture-pass',
                                                               lock['salt'])
        return {'new_table_absent': True, 'versions': versions, 'ciphertext_matches': True,
                'persona': persona['name'], 'memory_readable': True,
                'lock': {k: lock[k] for k in ('enabled', 'revision')}}
    finally:
        store.database.close()


def controlled_preservation_state(root, installation_id, vault, record):
    """The newer version's own preservation still carries the controlled table and its marker."""
    from portable_preservation import preserved_provider

    preservation = record['rollback']['preservation']
    provider = preserved_provider(
        root, installation_id, lambda keyref: data.slot_key(vault, installation_id, keyref))
    entry = provider.reopen(data.preservation_keyref(installation_id, preservation),
                            preservation['name'])
    assert digest(entry['blob']) == preservation['sha256'], 'the rollback preservation changed'
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    backup_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                      info=b'companion-postgres-backup-v1').derive(entry['key'])
    snapshot = postgres_transfer.unpack(entry['blob'], backup_key)
    block = snapshot['tables'].get(CONTROLLED_TABLE)
    assert block is not None, 'the newer preservation lost the controlled table'
    index = {name: position for position, name in enumerate(block['columns'])}
    rows = [[row[index['id']], row[index['body']]] for row in block['rows']]
    assert [PROBE_MARKER['id'], PROBE_MARKER['body']] in rows, 'the controlled marker was lost'
    versions = [int(row[0]) for row in snapshot['migrations']]
    assert CONTROLLED_VERSION in versions, 'the newer preservation lost the extra migration'
    return {'tables': len(snapshot['tables']), 'versions': versions,
            'controlled_table': CONTROLLED_TABLE, 'marker_rows': len(rows),
            'marker_sha256': digest(json.dumps(rows, ensure_ascii=False).encode('utf8'))}


def batch_d(run, evidence, args, vault, slots, worker, binaries):
    run.note('batch D is a controlled schema fixture: synthetic source program trees and an '
             'isolated migration copy, not two frozen EXEs')
    run.note('batch D controlled schema: companion_core.postgres.__file__ and the '
             'installation/data/service profile providers were replaced in this test process only '
             'and restored in finally')
    detail = {}
    base = evidence/'batch-d'
    root, state = new_root(run, evidence, vault, slots, 'batch-d', binaries, worker)
    installation_id = state['installation']
    port = state['port']
    server = run.server(root, binaries, port)
    detail['seed'] = run.step('seed', lambda: seed(root, installation_id, port))
    server.stop()
    old_app, new_app, installed = base/'app-old', base/'app-new', base/'app-installed'
    old_published = build_app(old_app, OLD_FILES, 'old')
    new_published = build_app(new_app, NEW_FILES, 'new')
    installed.mkdir()
    install_over(installed, old_app, old_published)
    detail['identities'] = {'old': old_published['sha256'], 'new': new_published['sha256']}

    # 1. The previous program prepares the clone with the checked-in schema only.
    prepared = run.step('prepare', lambda: service.prepare(
        root, installed, target_identity=new_published['sha256']))
    record = journal.load(root, required=True)
    clone = data.selected(root, installation_id)
    assert (clone['generation'] == record['clone']['generation']
            != record['origin']['selection']['generation'])
    detail['prepare'] = {'generation': prepared['generation'], 'carrier': prepared['carrier'],
                         'selection': journal.selection(clone)}
    detail['old_schema_clone'] = run.step('old_schema_clone', lambda: old_schema_clone(
        root, installed, vault, clone))

    # 2. A controlled migration copy lives beside this batch; the product source keeps only 001-.
    copy = controlled_schema_copy(base)
    detail['schema_copy'] = {key: copy[key] for key in ('scripts', 'version', 'script', 'table')}
    original_profile = desktop_profile()
    controlled_profile = replace(
        original_profile, extra_tables=set(original_profile.extra_tables) | {CONTROLLED_TABLE})
    detail['schema_copy']['extra_tables'] = sorted(controlled_profile.extra_tables)

    # 3. New schema: initialize the clone, write the marker, confirm, then prepare the way back.
    with controlled_schema_context(copy['file'], controlled_profile) as context:
        detail['schema_context'] = context
        install_over(installed, new_app, new_published)
        detail['initialize'] = run.step('new_schema_initialization', lambda: installation.initialize(
            root, binaries, installed/'resources/storage/worker.txt', None))
        state = data.state(root)
        port = state['port']
        server = run.server(root, binaries, port)
        try:
            detail['marker'] = run.step('controlled_marker', lambda: controlled_marker(
                root, vault, controlled_profile))
            detail['confirm'] = run.step('confirm', lambda: service.confirm(root, installed))
        finally:
            server.stop()
        detail['registry_after_confirm'] = run.step('registry_after_confirm', lambda: (
            registered(registry.load(root, installation_id))))
        verification.install(lambda request: {'session': 'source-integration-test-stub',
                                               'at': utcnow()})
        run.note('rollback verification channel: test stub, no production receipt, no Windows prompt')
        detail['rollback_prepare'] = run.step('rollback_prepare', lambda: service.rollback_prepare(
            root, app=installed))

    record = journal.load(root, required=True)
    sealed = record['rollback']['settings']
    assert sealed is not None and SEED_TEXT not in json.dumps(sealed)
    assert (detail['rollback_prepare']['current']['migrations_sha256']
            != record['origin']['data']['migrations_sha256']), 'the schema did not change'
    detail['rollback_preservation'] = {
        'name': record['rollback']['preservation']['name'],
        'generation': record['rollback']['preservation']['generation'], 'sealed': True,
        'migrations_changed': True}

    # 4. Back in the previous schema: the clone is refused, then only the old fallback is opened.
    clone = data.selected(root, installation_id)
    detail['old_schema_refusal'] = run.step('old_schema_refuses_clone', lambda: old_schema_refuses(
        root, installed, vault, clone))
    carrier = program.tree(root, record['carrier']['identity_sha256'])
    original_executing = service.executing_app
    service.executing_app = lambda: carrier
    try:
        detail['rollback'] = run.step('rollback', lambda: service.rollback(
            root, installed_app=installed))
    finally:
        service.executing_app = original_executing
    run.note('rollback ran with the previous schema only: the recorded fallback generation was the '
             'only one opened, and the newer clone was refused by storage_schema_upgrade_required')
    after_rollback = data.selected(root, installation_id)
    detail['selection_after_rollback'] = journal.selection(after_rollback)
    assert after_rollback['generation'] == record['origin']['selection']['generation']
    rolled = journal.load(root, required=True)
    assert rolled['phase'] == 'rolled-back', rolled['phase']
    assert rolled['rollback']['progress']['pointer_before'] == record['clone']['generation']
    detail['rollback_progress'] = {
        'phase': rolled['phase'],
        'pointer_before': rolled['rollback']['progress']['pointer_before'],
        'pointer_switched': rolled['rollback']['progress']['pointer_switched']}
    server = run.server(root, binaries, port)
    try:
        detail['fallback'] = run.step('fallback_state', lambda: controlled_fallback_state(
            root, state, after_rollback, detail['seed']['ciphertext']))
    finally:
        server.stop()

    # 5. The previous generation opens normally again, and the newer preservation kept the marker.
    detail['reopen'] = run.step('reopen', lambda: reopen(root, installed, state, run, binaries, port))
    detail['preservation'] = run.step('controlled_preservation', lambda: (
        controlled_preservation_state(root, installation_id, vault, record)))
    detail['program_after_rollback'] = run.step('program_after_rollback', lambda: program_state(
        installed, old_published))
    run.note(f'batch D controlled-schema installation {installation_id} verified')
    return detail


# -- batch E: the newer backend cannot run - seal the fault, then come back --


def archived_files(root, transaction, expected_directories=None):
    """Re-hash every archived file and prove the archived directory tree.

    The tree matters as much as the files: a stopped cluster keeps directories that hold no file
    (``pg_notify``, ``pg_replslot``, the ``pg_logical`` subfolders) and a copy whose tree was
    invented from the files alone cannot be started as a cluster. ``expected_directories`` is the
    directory set read from the installation right after the seal; it must match the manifest.
    """
    archive = emergency.archive_path(root, transaction)
    rows = json.loads((archive/emergency.MANIFEST).read_bytes())
    seen = set()
    for row in rows['files']:
        path = archive.joinpath(*row['path'].split('/'))
        content = path.read_bytes()
        assert len(content) == row['bytes'], 'an archived file changed size'
        assert digest(content) == row['sha256'], 'an archived file changed content'
        seen.add(row['path'])
    assert len(seen) == rows['count'], 'the manifest count and its rows disagree'
    directories = rows['directories']
    for name in directories:
        assert archive.joinpath(*name.split('/')).is_dir(), \
            f'the archive lost the directory {name}'
    found = sorted(path.relative_to(archive).as_posix()
                   for path in archive.rglob('*') if path.is_dir())
    assert found == directories, 'the archive holds a directory the manifest does not name'
    if expected_directories is not None:
        assert directories == list(expected_directories), \
            'the archived directory tree differs from the installed one'
    empty = [name for name in directories
             if not any(entry.is_file() for entry in archive.joinpath(*name.split('/')).iterdir())]
    assert empty, 'the archive has no empty directory; an invented tree cannot start a cluster'
    return {'files': rows['count'], 'bytes': rows['bytes'], 'directories': len(directories),
            'empty_directories': empty,
            'cluster_files': sum(1 for name in seen if name.startswith('database/')),
            'store_files': sum(1 for name in seen if name.startswith('store/')),
            'credentials': emergency.CREDENTIALS in seen,
            'excluded': sorted(emergency.EXCLUDED)}


def sealed_credentials(root, installation_id, transaction):
    """Decrypt the sealed slot values with the archive's own binding; neither side is printed."""
    archive = emergency.archive_path(root, transaction)
    blob = (archive/emergency.CREDENTIALS).read_bytes()
    binding = emergency.credentials_binding(installation_id, transaction)
    value = json.loads(emergency.crypt(blob, binding, decrypt=True).decode('utf8'))
    slots = value['slots']
    live = VaultCredentials()
    assert slots, 'no credential slot was sealed'
    assert all(live.get_password(VAULT, slot) == raw for slot, raw in slots.items()), \
        'a sealed credential value differs from the live slot'
    return {'slots': sorted(slots), 'matches_live_vault': True}


def pinned_state(root, installation_id):
    generations, programs = emergency.pinned(root, installation_id)
    return {'generations': sorted(generations, key=lambda item: item or ''),
            'programs': sorted(programs)}


def archived_new_schema(run, root, transaction, generation, base, binaries, installation_id):
    """Read the sealed cluster from a scratch copy: the newer build's rows must still be there.

    The archive itself is never opened by a server - it is copied out first - so a read-only check
    cannot change the fault material it is supposed to preserve.
    """
    archive = emergency.archive_path(root, transaction)
    scratch = base/'archive-verify'
    (scratch/'logs').mkdir(parents=True)
    shutil.copytree(archive/'database', scratch/'database')
    database = 'qiban_restore_'+generation
    credential = data.credentials(VaultCredentials(), installation_id)
    port = free_port()
    server = run.server(scratch, binaries, port)
    try:
        with psycopg.connect(host='127.0.0.1', port=port, dbname=database, user='qiban_setup',
                             password=credential['bootstrap_password'],
                             connect_timeout=20) as db:
            rows = [list(row) for row in db.execute(
                'SELECT id, body FROM upgrade_probe ORDER BY id')]
            versions = [int(row[0]) for row in db.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
    finally:
        server.stop()
    assert [PROBE_MARKER['id'], PROBE_MARKER['body']] in rows, \
        'the sealed cluster lost the newer schema row'
    assert CONTROLLED_VERSION in versions, 'the sealed cluster lost the controlled migration'
    return {'database': database, 'rows': rows, 'controlled_version': CONTROLLED_VERSION,
            'archive_untouched': (archive/'database').is_dir()}


def cleanup_refused(root):
    """A finished emergency transaction is never cleaned automatically."""
    try:
        service.core_cleanup(root)
    except AssistantError as error:
        assert error.code == 'upgrade_state', error.code
        return {'refused': True, 'code': error.code}
    raise AssertionError('a finished emergency rollback must not be cleaned automatically')


def held_platforms(root, state, selection):
    """The recovery rules of the emergency way back hold every platform, without new settings."""
    credential = data.credentials(VaultCredentials(), state['installation'])
    key = data.slot_key(VaultCredentials(), state['installation'], selection['credential_id'])
    options = {'host': '127.0.0.1', 'port': state['port'], 'dbname': selection['database'],
               'user': 'qiban_setup', 'password': credential['bootstrap_password'],
               'connect_timeout': 5}
    store = PostgresStore(Path(selection['store']), options, key, profile=desktop_profile())
    try:
        hold = store.state(HOLD_KEY)
        session = store.state(SESSION_KEY)
        assert hold and hold['platforms'], 'platforms were not held after the emergency rollback'
        assert session['held'] == hold['platforms'], 'the session does not hold every platform'
        return {'held': hold['platforms'], 'generation': hold['generation']}
    finally:
        store.database.close()


def live_clone_row(state, generation, installation_id):
    """The live newer-schema database is untouched: the pointer moved, no database was dropped."""
    database = 'qiban_restore_'+generation
    credential = data.credentials(VaultCredentials(), installation_id)
    with psycopg.connect(host='127.0.0.1', port=state['port'], dbname=database,
                         user='qiban_setup', password=credential['bootstrap_password'],
                         connect_timeout=20) as db:
        rows = [list(row) for row in db.execute('SELECT id, body FROM upgrade_probe ORDER BY id')]
    assert [PROBE_MARKER['id'], PROBE_MARKER['body']] in rows, 'the live clone lost its newer row'
    return {'database': database, 'rows': rows}


def batch_e(run, evidence, args, vault, slots, worker, binaries):
    run.note('batch E reuses the controlled fixture of batch D for its schema, and never starts the '
             'newer maintenance backend: no rollback-prepare and no new preservation is produced')
    detail = {}
    base = evidence/'batch-e'
    root, state = new_root(run, evidence, vault, slots, 'batch-e', binaries, worker)
    installation_id = state['installation']
    port = state['port']
    server = run.server(root, binaries, port)
    detail['seed'] = run.step('seed', lambda: seed(root, installation_id, port))
    server.stop()
    old_app, new_app, installed = base/'app-old', base/'app-new', base/'app-installed'
    old_published = build_app(old_app, OLD_FILES, 'old')
    new_published = build_app(new_app, NEW_FILES, 'new')
    installed.mkdir()
    install_over(installed, old_app, old_published)
    detail['identities'] = {'old': old_published['sha256'], 'new': new_published['sha256']}

    prepared = run.step('prepare', lambda: service.prepare(
        root, installed, target_identity=new_published['sha256']))
    record = journal.load(root, required=True)
    clone = data.selected(root, installation_id)
    assert (clone['generation'] == record['clone']['generation']
            != record['origin']['selection']['generation'])
    detail['prepare'] = {'generation': prepared['generation'], 'carrier': prepared['carrier'],
                         'selection': journal.selection(clone)}

    # The newer build writes a row its predecessor cannot read, and its backend is never asked to
    # preserve anything: that is the fault this batch is about.
    copy = controlled_schema_copy(base)
    original_profile = desktop_profile()
    controlled_profile = replace(
        original_profile, extra_tables=set(original_profile.extra_tables) | {CONTROLLED_TABLE})
    with controlled_schema_context(copy['file'], controlled_profile):
        install_over(installed, new_app, new_published)
        run.step('new_schema_initialization', lambda: installation.initialize(
            root, binaries, installed/'resources/storage/worker.txt', None))
        state = data.state(root)
        port = state['port']
        server = run.server(root, binaries, port)
        try:
            detail['marker'] = run.step('controlled_marker', lambda: controlled_marker(
                root, vault, controlled_profile))
            detail['confirm'] = run.step('confirm', lambda: service.confirm(root, installed))
        finally:
            server.stop()

    record = journal.load(root, required=True)
    assert record['phase'] == 'confirmed', record['phase']
    verification.install(lambda request: {'session': 'source-integration-test-stub',
                                          'at': utcnow()})
    run.note('emergency verification channel: test stub, no production receipt, no Windows prompt')
    carrier = program.tree(root, record['carrier']['identity_sha256'])
    original_executing = service.executing_app
    service.executing_app = lambda: carrier
    try:
        detail['seal'] = run.step('emergency_seal', lambda: service.emergency_seal(root))
        assert detail['seal']['phase'] == 'emergency'
        assert detail['seal']['material'] == 'fault-material'
        assert detail['seal']['restorable'] is False
        detail['archive'] = run.step('archive_verified', lambda: archived_files(
            root, record['id'], emergency.contents(root)[1]))
        detail['sealed_credentials'] = run.step('sealed_credentials', lambda: sealed_credentials(
            root, installation_id, record['id']))
        detail['pins'] = run.step('pins', lambda: pinned_state(root, installation_id))
        assert detail['pins']['generations'] == [clone['generation']]
        assert detail['pins']['programs'] == [record['target']['identity_sha256']]
        detail['seal_again'] = run.step('emergency_seal_again',
                                        lambda: service.emergency_seal(root))
        assert detail['seal_again']['already'] is True
        detail['switch'] = run.step('emergency_rollback', lambda: service.emergency_rollback(
            root, installed_app=installed))
        detail['switch_again'] = run.step('emergency_rollback_again',
                                          lambda: service.emergency_rollback(
                                              root, installed_app=installed))
        assert detail['switch_again']['already'] is True
    finally:
        service.executing_app = original_executing
    assert detail['switch']['phase'] == 'emergency-rolled-back'
    assert detail['switch']['removed'] == ['tools/new-only.txt']
    assert detail['switch']['program_files'] == 'complete'
    assert any('故障新版最后修改的本机设置没有保留' in note for note in detail['switch']['notes'])

    after = data.selected(root, installation_id)
    detail['selection_after_emergency'] = journal.selection(after)
    assert after['generation'] == record['origin']['selection']['generation']
    rolled = journal.load(root, required=True)
    assert rolled['emergency']['progress']['pointer_switched'] is True
    detail['emergency_record'] = {
        'phase': rolled['phase'],
        'archive': rolled['emergency']['archive']['transaction'],
        'files': rolled['emergency']['archive']['count'],
        'target_manifest': rolled['emergency']['target']['identity_sha256']}
    detail['program_after_emergency'] = run.step('program_after_emergency', lambda: program_state(
        installed, old_published))

    server = run.server(root, binaries, port)
    try:
        detail['fallback'] = run.step('fallback_state', lambda: controlled_fallback_state(
            root, state, after, detail['seed']['ciphertext']))
        detail['held'] = run.step('platforms_held', lambda: held_platforms(root, state, after))
        detail['live_clone'] = run.step('live_clone_untouched', lambda: live_clone_row(
            state, clone['generation'], installation_id))
    finally:
        server.stop()
    detail['archived_new_schema'] = run.step('archived_new_schema', lambda: archived_new_schema(
        run, root, record['id'], clone['generation'], base, binaries, installation_id))
    detail['cleanup_refused'] = run.step('cleanup_refused', lambda: cleanup_refused(root))
    detail['reopen'] = run.step('reopen', lambda: reopen(root, installed, state, run, binaries, port))
    detail['archive_after'] = run.step('archive_after', lambda: archived_files(
        root, record['id']))
    assert detail['archive_after'] == detail['archive'], 'the sealed archive changed after the switch'
    run.note('the fallback carries no controlled table while the sealed cluster copy still holds '
             'the newer schema row; nothing was cleaned after the emergency rollback')
    run.note(f'batch E controlled-schema installation {installation_id} verified')
    return detail


# -- batch F: a preparation interrupted before the switch recovers by itself --


def database_state(state, *, dropped, clone):
    """The abandoned clone's database is gone, the recovered one exists, the roles are unchanged."""
    credential = data.credentials(VaultCredentials(), state['installation'])
    options = {'host': '127.0.0.1', 'port': state['port'], 'dbname': 'postgres',
               'user': 'qiban_setup', 'password': credential['bootstrap_password'],
               'connect_timeout': 5}
    with psycopg.connect(**options) as db:
        names = {row[0] for row in db.execute('SELECT datname FROM pg_database')}
        roles = {row[0]: list(row[1:]) for row in db.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
            "WHERE rolname IN ('qiban_owner','qiban_runtime')")}
    assert dropped not in names, 'the abandoned clone database is still there'
    assert clone in names, 'the recovered clone database is missing'
    assert roles.get('qiban_owner') == [False, False, False, False], 'the owner role changed'
    assert roles.get('qiban_runtime') == [True, False, False, False], 'the runtime role changed'
    return {'abandoned_dropped': True, 'recovered_present': True, 'roles': sorted(roles)}


def batch_f(run, evidence, args, vault, slots, worker, binaries):
    run.note('batch F asks one question on the pattern of batches A/B: a preparation interrupted '
             'after the clone exists and before the pointer moved must be recoverable by the same '
             'target, without a separate abort step')
    detail = {}
    base = evidence/'batch-f'
    root, state = new_root(run, evidence, vault, slots, 'batch-f', binaries, worker)
    installation_id = state['installation']
    port = state['port']
    server = run.server(root, binaries, port)
    detail['seed'] = run.step('seed', lambda: seed(root, installation_id, port))
    server.stop()
    old_app, new_app, installed = base/'app-old', base/'app-new', base/'app-installed'
    old_published = build_app(old_app, OLD_FILES, 'old')
    new_published = build_app(new_app, NEW_FILES, 'new')
    installed.mkdir()
    install_over(installed, old_app, old_published)
    before = data.selected(root, installation_id)
    detail['identities'] = {'old': old_published['sha256'], 'new': new_published['sha256']}

    def interrupt(facts):
        raise AssistantError('source_integration_before_commit',
                             '注入：提交前中断，指针没有切换')

    def interrupted():
        try:
            service.prepare(root, installed, target_identity=new_published['sha256'],
                            before_commit=interrupt)
        except AssistantError as error:
            assert error.code == 'source_integration_before_commit', error.code
            return {'code': error.code}
        raise AssertionError('the injected interruption did not happen')

    detail['interrupted'] = run.step('prepare_interrupted_before_commit', interrupted)
    stranded = journal.load(root, required=True)
    pointer = data.selected(root, installation_id)
    assert stranded['phase'] == 'staging', stranded['phase']
    assert pointer['generation'] == before['generation'], 'the pointer moved anyway'
    abandoned = stranded['plan']
    # The clone's credential slot was written before the commit hook; this batch created it, so the
    # summary cleanup of the driver has to know its id (the directory is gone with the clone).
    slots.append(abandoned['generation'])
    detail['stranded'] = {'phase': stranded['phase'], 'transaction': stranded['id'],
                          'clone': abandoned['generation'],
                          'pointer': before['generation'],
                          'store_exists': (root/'generations'/abandoned['generation']).is_dir()}

    detail['recovery'] = run.step('prepare_recovers_the_same_target', lambda: service.prepare(
        root, installed, target_identity=new_published['sha256']))
    assert detail['recovery']['abandoned']['generation'] == abandoned['generation']
    recovered = journal.load(root, required=True)
    assert recovered['phase'] == 'staged', recovered['phase']
    assert recovered['id'] != stranded['id'], 'the abandoned transaction was continued'
    assert recovered['clone']['generation'] != abandoned['generation']
    assert not (root/'generations'/abandoned['generation']).exists(), \
        'the abandoned clone directory is still there'
    detail['recovered'] = {'phase': recovered['phase'], 'transaction': recovered['id'],
                           'clone': recovered['clone']['generation'],
                           'abandoned': abandoned['generation']}

    server = run.server(root, binaries, port)
    try:
        detail['databases'] = run.step('databases_after_recovery', lambda: database_state(
            state, dropped=abandoned['database'], clone=recovered['clone']['database']))
        detail['old_generation'] = run.step('old_generation_untouched', lambda: (
            old_generation_state(root, state, before, detail['seed']['ciphertext'])))
    finally:
        server.stop()

    frozen = (root/'upgrade'/'transaction.json').read_bytes()
    detail['retry'] = run.step('staged_retry_keeps_the_transaction', lambda: service.prepare(
        root, installed, target_identity=new_published['sha256']))
    assert detail['retry']['resumed'] is True
    assert detail['retry']['transaction'] == recovered['id']
    assert detail['retry']['generation'] == recovered['clone']['generation']
    assert (root/'upgrade'/'transaction.json').read_bytes() == frozen, 'the retry wrote the record'
    run.note('the interrupted preparation recovered by itself: the abandoned clone was dropped '
             '(database, directory, allocation entry), the same target rebuilt and reached staged, '
             'a further retry kept that transaction and clone, and the old generation kept its '
             'roles and ciphertext')
    run.note(f'batch F installation {installation_id} verified')
    return detail


if __name__ == '__main__':
    raise SystemExit(main())
