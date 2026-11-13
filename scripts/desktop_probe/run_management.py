"""Verify native desktop management using only a disposable installation."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))

from credential_vault import VaultCredentials
from run_storage import StackHost
from storage_worker import VAULT

from companion_core.process_scope import ProcessScope


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def measure_checkpoint(root, evidence):
    """Diagnostic only: measure a checkpoint before the unchanged normal stop."""
    state=read(root/'installation.json')
    credential=json.loads(VaultCredentials().get_password(VAULT,state['installation']))
    result={'completed':False}
    started=time.monotonic()
    try:
        with psycopg.connect(host='127.0.0.1',port=state['port'],user='qiban_setup',
                password=credential['bootstrap_password'],dbname='qiban',autocommit=True,
                connect_timeout=5,options='-c statement_timeout=60000') as db:
            columns='num_requested,num_done,write_time,sync_time,buffers_written'
            result['before']=list(db.execute('SELECT '+columns+' FROM pg_stat_checkpointer').fetchone())
            db.execute('CHECKPOINT')
            db.execute('SELECT pg_stat_clear_snapshot()')
            result['after']=list(db.execute('SELECT '+columns+' FROM pg_stat_checkpointer').fetchone())
            result['completed']=True
    except Exception as error:
        # Never write connection options, database exception text or credentials.
        result['error_type']=type(error).__name__
        raise RuntimeError('Diagnostic checkpoint failed; inspect checkpoint-result.json') from None
    finally:
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        (evidence/'checkpoint-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def measure_direct_stop(root, evidence, host):
    """Observe one fast shutdown for up to 60s, without the worker's 10s cutoff.

    This intentionally looks like database loss to the host. It is a diagnostic,
    never normal application shutdown acceptance or a product timeout override.
    """
    spec=read(root/'postgres-spec.json')
    data=Path(spec['DataDirectory']).resolve(strict=True)
    assert data.is_relative_to(root.resolve())
    assert (data/'qiban-probe-owner').read_text().strip()==spec['OwnerToken']
    observed=read(Path(spec['LogDirectory'])/'database-process.json')
    assert observed['owned'] and int((data/'postmaster.pid').read_text().splitlines()[0])==observed['pid']
    assert host.process.poll() is None
    result={'completed':False,'normal_application_shutdown':False}
    started=time.monotonic()
    try:
        with (evidence/'direct-stop.log').open('wb') as log:
            stopped=subprocess.run([str(Path(spec['BinDirectory'])/'pg_ctl.exe'),'-D',str(data),
                '-m','fast','-w','-t','60','stop'],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                timeout=65,creationflags=subprocess.CREATE_NO_WINDOW)
        result['control_exit_code']=stopped.returncode
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        result['completed']=stopped.returncode==0 and not (data/'postmaster.pid').exists()
        assert result['completed'],'Direct diagnostic shutdown did not finish'
        result['host_exit_code']=host.process.wait(timeout=15)
        assert result['host_exit_code']==1,'Unexpected host outcome after external database stop'
    finally:
        result.setdefault('elapsed_seconds',round(time.monotonic()-started,3))
        (evidence/'direct-stop-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',required=True,type=Path)
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--extra-test',action='append',default=[])
    parser.add_argument('--only-extra-tests',action='store_true',help='Run only explicitly selected pytest files after native management checks')
    parser.add_argument('--test-timeout',type=int,default=75)
    diagnostic=parser.add_mutually_exclusive_group()
    diagnostic.add_argument('--measure-checkpoint',action='store_true',
                        help='Diagnostic run: measure a manual checkpoint before normal stop; not ordinary shutdown acceptance')
    diagnostic.add_argument('--measure-direct-stop',action='store_true',
                        help='Diagnostic run: observe one external fast shutdown for at most 60s; not application shutdown acceptance')
    args=parser.parse_args()
    if not 1 <= args.test_timeout <= 3540:
        parser.error('--test-timeout must be between 1 and 3540 seconds')
    if args.only_extra_tests and not args.extra_test:
        parser.error('--only-extra-tests requires at least one --extra-test')
    exe,evidence=args.exe.resolve(strict=True),args.evidence.resolve()
    evidence.mkdir(parents=True,exist_ok=False)
    root=evidence/'中文管理测试'
    report={'passed':False,'checkpoint_diagnostic':args.measure_checkpoint,'direct_stop_diagnostic':args.measure_direct_stop}
    scope,host=ProcessScope(),None
    try:
        with (evidence/'native-test.log').open('wb') as log:
            process=subprocess.Popen([str(exe),'--management-tests',str(root),str(exe.parent),str(evidence/'native')],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW|0x4)
            scope.attach(process)
            scope.resume()
            assert process.wait(timeout=180)==0,'Native management tests failed'
        native=read(evidence/'native/management-result.json')
        assert native['passed']
        report['native_cases']=native['cases']
        scope.close()
        host=StackHost(exe,root/'stack-spec.json',evidence/'stack',read(root/'postgres-spec.json'),root/'logs',
                       deadline_seconds=args.test_timeout+30)
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[2]/'src'),'QIBAN_DESKTOP_TEST_ROOT':str(root)}
        with (evidence/'authorization-tests.log').open('wb') as log:
            base_tests=[] if args.only_extra_tests else ['tests_postgres/test_desktop_management.py','tests_postgres/test_desktop_runtime.py']
            result=subprocess.run([sys.executable,'-m','pytest',*base_tests,*args.extra_test,'-q',
                '--junitxml='+str(evidence/'authorization-tests.xml')],env=env,
                cwd=Path(__file__).resolve().parents[2],stdout=log,stderr=log,timeout=args.test_timeout,
                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode==0,'Authorization tests failed; inspect authorization-tests.log'
        if args.measure_checkpoint:
            report['checkpoint']=measure_checkpoint(root,evidence)
        if args.measure_direct_stop:
            report['direct_stop']=measure_direct_stop(root,evidence,host)
        else:
            host.stop()
        report['passed']=True
    finally:
        if host:
            host.close()
        scope.close()
        if (root/'installation.json').exists():
            installation=read(root/'installation.json')['installation']
            vault=VaultCredentials()
            if vault.get_password(VAULT,installation):
                vault.delete_password(VAULT,installation)
            report['test_credentials_removed']=vault.get_password(VAULT,installation) is None
        (evidence/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()
