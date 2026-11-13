"""Frozen old-to-new installation upgrade, including host death before grants."""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

import psycopg

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))

from credential_vault import VaultCredentials
from run_installation import InstallHost
from run_storage import StackHost, ciphertext
from storage_worker import VAULT

from companion_core.media_originals import MediaOriginals
from companion_core.postgres import PostgresStore, verify_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', required=True, type=Path)
    parser.add_argument('--new', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    root = evidence / '中文升级资料'
    vault, hosts = VaultCredentials(), []
    report = {'passed': False, 'cases': [], 'model_calls': 0, 'messages_sent': 0}
    def read(p):
        return json.loads(p.read_text(encoding='utf8'))
    def install(application, name, pause=''):
        host = InstallHost(application / 'Qiban.exe', application, root, evidence / name, os.environ.copy(), pause)
        hosts.append(host)
        if not pause:
            host.wait()
            host.close()
        return host
    def stack(application, name):
        host = StackHost(application / 'Qiban.exe', root / 'stack-spec.json', evidence / name,
                         read(root / 'postgres-spec.json'), root / 'logs', deadline_seconds=180)
        hosts.append(host)
        return host
    try:
        old, new = args.old.resolve(), args.new.resolve()
        install(old, 'old-install')
        identity = read(root / 'installation.json')['installation']
        original_credential = vault.get_password(VAULT, identity)
        credential = json.loads(original_credential)
        options = {**read(root / 'storage-config.json')['database'], 'password': credential['database_password']}
        key = base64.b64decode(credential['data_key'])
        host = stack(old, 'old-read')
        with psycopg.connect(**options) as db:
            assert db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 11
        store = PostgresStore(root / 'store', options, key)
        try:
            persona = store.read('persona')['data']
            persona['interests'] = ['旧版本升级后必须保留']
            store.write('persona', persona, store.version('persona'), 'upgrade-test')
            before = ciphertext(options)
        finally:
            store.database.close()
        host.stop()
        host.close()
        interrupted = install(new, 'upgrade-interrupted', pause='schema-migrated')
        interrupted.crash(interrupted.pause('schema-migrated'))
        interrupted.close()
        assert read(root / 'installation.json')['phase'] == 'ready'
        report['cases'].append('actual_host_death_after_schema_commit_before_runtime_grants')
        install(new, 'upgrade-resumed')
        assert read(root / 'installation.json')['installation'] == identity
        assert vault.get_password(VAULT, identity) == original_credential
        host = stack(new, 'upgraded-read')
        store = PostgresStore(root / 'store', options, key)
        try:
            verify_schema(store.database)
            assert ciphertext(options) == before
            assert store.read('persona')['data']['interests'] == ['旧版本升级后必须保留']
            assert MediaOriginals(store).listing()['settings']['days'] == 30
            with psycopg.connect(**options) as db:
                assert db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 12
                assert db.execute("SELECT has_table_privilege(current_user,'media_originals','SELECT,INSERT,UPDATE,DELETE')").fetchone()[0]
                assert not db.execute("SELECT has_table_privilege(current_user,'schema_migrations','UPDATE')").fetchone()[0]
                assert db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone() == (False, False, False)
        finally:
            store.database.close()
        host.stop()
        host.close()
        report['cases'].append('ready_install_upgraded_preserving_identity_ciphertext_and_restricted_runtime')
        install(new, 'repeat-upgrade')
        host = stack(new, 'repeat-read')
        assert ciphertext(options) == before
        host.stop()
        host.close()
        report['cases'].append('repeat_start_keeps_existing_data')
        report['passed'] = True
    finally:
        for host in reversed(hosts):
            host.close()
        if (root / 'installation.json').exists():
            identity = read(root / 'installation.json')['installation']
            if vault.get_password(VAULT, identity):
                vault.delete_password(VAULT, identity)
            report['test_credentials_removed'] = vault.get_password(VAULT, identity) is None
        (evidence / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
