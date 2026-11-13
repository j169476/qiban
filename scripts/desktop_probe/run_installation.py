"""Exercise fresh installation and actual host death at durable setup boundaries."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))

from credential_vault import VaultCredentials
from run_lifecycle import api
from run_storage import StackHost, ciphertext
from storage_worker import VAULT

from companion_core.postgres import PostgresStore
from companion_core.process_scope import ProcessScope


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


class InstallHost:
    def __init__(self, exe, application, root, evidence, env, pause=''):
        evidence.mkdir()
        self.evidence, self.root = evidence, root
        self.scope = ProcessScope()
        try:
            self.process = subprocess.Popen([str(exe), '--install-host', str(root), str(application), str(evidence), pause],
                env=env, cwd=evidence, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            self.scope.attach(self.process)
            self.scope.resume()
        except BaseException:
            self.scope.close()
            raise

    def wait(self, failure=None):
        code = self.process.wait(timeout=150)
        state = read(self.evidence/'state.json')
        assert code == (1 if failure else 0), state
        assert state['state'] == ('failed' if failure else 'stopped'), state
        assert state['detail'] == (failure or 'graceful_stop'), state

    def pause(self, phase):
        deadline = time.monotonic()+80
        while time.monotonic() < deadline:
            try:
                state = read(self.root/'installation-status.json')
                if state['phase'] == phase:
                    return state
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if self.process.poll() is not None:
                raise RuntimeError(str(read(self.evidence/'state.json')))
            time.sleep(.05)
        raise TimeoutError('Initialization checkpoint not reached')

    def crash(self, phase):
        pids = [phase['pid']]
        if 'child' in phase:
            pids.append(phase['child'])
        pidfile = self.root/'database/postmaster.pid'
        if pidfile.exists():
            pids.append(int(pidfile.read_text(encoding='utf8').splitlines()[0]))
        handles = []
        try:
            for pid in pids:
                handle = api.OpenProcess(0x100000, False, pid)
                if handle:
                    handles.append(handle)
            assert handles
            self.process.kill()
            self.process.wait(timeout=10)
            assert all(api.WaitForSingleObject(handle, 10000) == 0 for handle in handles)
        finally:
            for handle in handles:
                api.CloseHandle(handle)

    def close(self):
        self.scope.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop', type=Path, required=True)
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--postgres', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--exe-name', default='Qiban.DesktopProbe.exe')
    parser.add_argument('--reserved-port', type=int)
    parser.add_argument('--phases', nargs='*', default=['planned','vault-written','initdb-running','initdb-finished',
        'cluster','database-created','schema-migrated','records-written','validated','configs-written'])
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    application = evidence/'中文程序'
    shutil.copytree(args.desktop.resolve(), application)
    shutil.copytree(args.storage.resolve(), application/'resources/storage')
    shutil.copytree(args.postgres.resolve(), application/'resources/postgres')
    exe = application/args.exe_name
    env = {**os.environ, 'PATH':str(Path(os.environ['SystemRoot'])/'System32'),
           'PYTHONHOME':str(evidence/'no-python'), 'PYTHONPATH':str(evidence/'no-source')}
    report, hosts, roots = {'passed':False, 'cases':[]}, [], []
    vault = VaultCredentials()
    before = {p.relative_to(application).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
              for p in application.rglob('*') if p.is_file()}
    def install(root, name, pause='', failure=None):
        host = InstallHost(exe, application, root, evidence/name, env, pause)
        hosts.append(host)
        if not pause:
            host.wait(failure)
            host.close()
        return host
    def connection(root, admin=False):
        state = read(root/'installation.json')
        credential = json.loads(vault.get_password(VAULT,state['installation']))
        config = read(root/'storage-config.json')
        options = {**config['database'], 'password':credential['database_password']}
        if admin:
            options.update(user='qiban_setup', password=credential['bootstrap_password'])
        return options, base64.b64decode(credential['data_key'])
    def open_stack(root, name):
        host = StackHost(exe, root/'stack-spec.json', evidence/name, read(root/'postgres-spec.json'), root/'logs', env=env, cwd=evidence)
        hosts.append(host)
        return host
    try:
        root = evidence/'中文用户资料'
        roots.append(root)
        install(root, 'fresh')
        assert read(root/'installation.json')['phase'] == 'ready'
        original_id = read(root/'installation.json')['installation']
        original_vault = vault.get_password(VAULT, original_id)
        host = open_stack(root,'fresh-read')
        options, key = connection(root)
        with psycopg.connect(**options, autocommit=True) as db:
            assert db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=current_user').fetchone() == (False,False,False,False)
            try:
                db.execute('CREATE TABLE forbidden_test(id integer)')
                raise AssertionError('Runtime unexpectedly allowed schema creation')
            except psycopg.errors.InsufficientPrivilege:
                pass
        store = PostgresStore(root/'store', options, key)
        try:
            persona = store.read('persona')['data']
            persona['interests'] = ['初始化恢复校验']
            store.write('persona',persona,store.version('persona'),'installation-test')
            saved_cipher = ciphertext(options)
        finally:
            store.database.close()
        host.stop()
        host.close()
        report['cases'].append('fresh_unicode_install_and_restricted_runtime_write')
        install(root,'reopen')
        assert vault.get_password(VAULT,original_id) == original_vault
        host = open_stack(root,'reopen-read')
        assert ciphertext(options) == saved_cipher
        host.stop()
        host.close()
        report['cases'].append('reopen_preserves_identity_credentials_and_edited_ciphertext')
        # The completed store must fail rather than generate replacement credentials.
        vault.delete_password(VAULT,original_id)
        install(root,'missing-vault',failure='storage_vault_missing')
        assert vault.get_password(VAULT,original_id) is None
        vault.set_password(VAULT,original_id,original_vault)
        report['cases'].append('completed_install_missing_vault_refused')
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',read(root/'installation.json')['port']))
            occupied.listen()
            install(root,'port-conflict',failure='database_port_in_use')
        assert vault.get_password(VAULT,original_id) == original_vault
        report['cases'].append('port_conflict_refused_without_new_identity')
        if args.reserved_port is not None:
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', args.reserved_port))
                except OSError as error:
                    assert error.winerror == 10013
                else:
                    raise AssertionError('Requested test port is not OS-denied')
            saved_state=read(root/'installation.json')
            saved_state['port']=args.reserved_port
            (root/'installation.json').write_text(json.dumps(saved_state),encoding='utf8')
            host=install(root,'reserved-pause',pause='port-reassigned')
            status=host.pause('port-reassigned')
            recovered_port=read(root/'installation.json')['port']
            assert recovered_port!=args.reserved_port
            host.crash(status)
            host.close()
            install(root,'reserved-resume')
            assert read(root/'installation.json')['port']==recovered_port
            assert read(root/'installation.json')['installation']==original_id
            assert vault.get_password(VAULT,original_id)==original_vault
            host=open_stack(root,'reserved-read')
            options,key=connection(root)
            assert ciphertext(options)==saved_cipher
            host.stop()
            host.close()
            report['cases'].append('reserved_port_reassigned_crash_resume_preserves_database')
        manifest = application/'resources/postgres/bin/postgres.exe.manifest'
        saved_manifest = manifest.read_bytes()
        try:
            manifest.unlink()
            install(root,'missing-pg-resource',failure='database_bundle_invalid')
        finally:
            manifest.write_bytes(saved_manifest)
        report['cases'].append('postgres_resource_missing_refused')
        wrong = json.loads(original_vault)
        wrong['data_key'] = base64.b64encode(bytes(range(32))).decode()
        vault.set_password(VAULT,original_id,json.dumps(wrong))
        try:
            install(root,'wrong-key',failure='storage_integrity')
        finally:
            vault.set_password(VAULT,original_id,original_vault)
        report['cases'].append('completed_install_wrong_key_refused')
        for phase in args.phases:
            current = evidence/('中断-'+phase)
            roots.append(current)
            host = install(current,'pause-'+phase,pause=phase)
            status = host.pause(phase)
            initial = read(current/'installation.json')['installation']
            credential = vault.get_password(VAULT,initial)
            prior_cipher = None
            prior_owner = None
            if phase in {'records-written','validated'}:
                account = json.loads(credential)
                prior_cipher = ciphertext({'host':'127.0.0.1','port':read(current/'installation.json')['port'],
                    'user':'qiban_setup','password':account['bootstrap_password'],'dbname':'qiban'})
                with psycopg.connect(host='127.0.0.1',port=read(current/'installation.json')['port'],
                        user='qiban_setup',password=account['bootstrap_password'],dbname='qiban') as db:
                    prior_owner = db.execute("SELECT value FROM state WHERE key='desktop-owner'").fetchone()[0]
            if phase == 'vault-written':
                install(current,'concurrent-refused',failure='installation_busy')
                assert vault.get_password(VAULT,initial) == credential
                report['cases'].append('concurrent_initialization_refused')
            host.crash(status)
            host.close()
            install(current,'recover-'+phase)
            assert read(current/'installation.json')['installation'] == initial
            if credential is not None:
                assert vault.get_password(VAULT,initial) == credential
            assert read(current/'installation.json')['phase'] == 'ready'
            live = open_stack(current,'read-'+phase)
            recovery_options, _ = connection(current)
            recovered_cipher = ciphertext(recovery_options)
            assert recovered_cipher
            if prior_cipher is not None:
                assert recovered_cipher == prior_cipher
            with psycopg.connect(**recovery_options) as db:
                owner=db.execute("SELECT value FROM state WHERE key='desktop-owner'").fetchone()[0]
                assert owner
                if prior_owner is not None:
                    assert owner==prior_owner,'Recovery replaced the desktop administrator'
            live.stop()
            live.close()
            report['cases'].append('host_kill_and_resume_'+phase)
        # Window runs the three-component preparation -> database -> storage chain.
        profile = evidence/'首次窗口'
        roots.append(profile/'data/installation')
        scope = ProcessScope()
        try:
            window = subprocess.Popen([str(exe),'--managed-install','--smoke','--data-dir',str(profile)],
                env=env,cwd=evidence,stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            scope.attach(window)
            scope.resume()
            # Two managed starts may each use the existing 180+30+30s budgets;
            # the remaining UI assertions retain their short individual limits.
            assert window.wait(timeout=600) == 0
            assert read(profile/'result.json')['passed']
            report['cases'].append('fresh_window_manages_initialization_and_storage')
        finally:
            scope.close()
        after = {p.relative_to(application).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in application.rglob('*') if p.is_file()}
        assert before == after
        report['cases'].append('application_resources_unchanged')
        for current in roots:
            if not (current/'installation.json').exists():
                continue
            credential = json.loads(vault.get_password(VAULT,read(current/'installation.json')['installation']))
            secrets_to_check = [value.encode('ascii') for value in credential.values()]
            for file in current.rglob('*'):
                if file.is_file() and file.suffix in {'.json','.log','.conf','.txt'}:
                    assert not any(secret in file.read_bytes() for secret in secrets_to_check), 'Secret in log/config'
        report['cases'].append('credentials_absent_from_logs_and_configuration')
        report['passed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            for host in hosts:
                host.close()
        finally:
            cleared = []
            for root in roots:
                if (root/'installation.json').exists():
                    identity = read(root/'installation.json')['installation']
                    if vault.get_password(VAULT,identity) is not None:
                        vault.delete_password(VAULT,identity)
                    cleared.append(vault.get_password(VAULT,identity) is None)
            report['test_credentials_removed'] = all(cleared)
            (evidence/'installation-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
