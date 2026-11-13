"""Prepare a disposable locked profile, then leave its real window for human Windows verification."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from install_windows import user_sid
from run_management import ProcessScope, read
from run_storage import StackHost, ciphertext
from storage_worker import credentials, open_store

from qiban.desktop_management import DesktopManagement


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    exe,evidence=args.exe.resolve(strict=True),args.evidence.resolve()
    evidence.mkdir(parents=True,exist_ok=False)
    profile=evidence/'人工验证资料'
    root=profile/'data/installation'
    scope,host=ProcessScope(),None
    report={'state':'preparing','profile':str(profile),'application':str(exe)}
    try:
        with (evidence/'initialize.log').open('wb') as log:
            process=subprocess.Popen([str(exe.parent/'resources/storage/Qiban.Storage.exe'),'--initialize',str(root),
                '--postgres',str(exe.parent/'resources/postgres/bin')],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW|0x4)
            scope.attach(process)
            scope.resume()
            assert process.wait(timeout=150)==0
        scope.close()
        host=StackHost(exe,root/'stack-spec.json',evidence/'setup-stack',read(root/'postgres-spec.json'),root/'logs')
        store=open_store(root/'storage-config.json')
        try:
            installation=read(root/'installation.json')['installation']
            service=DesktopManagement(store,installation,user_sid())
            record=store.read('persona')
            service.admin.patch_config(service.request(),'persona',{'name':'密码恢复人工验证'},record['version'])
            service.enable_lock('Qiban-Test-Old-2026!')
            service.lock()
            _,password=credentials(installation)
            config=read(root/'storage-config.json')
            report.update(installation=installation,lock_before=service.status(),
                content_sha256=ciphertext({**config['database'],'password':password}))
        finally:
            store.database.close()
        host.stop()
    finally:
        if host:
            host.close()
        scope.close()
    # Deliberately persistent interactive window; its own Jobs own all backend processes.
    window=subprocess.Popen([str(exe),'--data-dir',str(profile),'--recovery-inspect'],cwd=evidence,
        stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    report.update(state='validating_recovery_visibility',pid=window.pid)
    (evidence/'manual-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    deadline=time.monotonic()+140
    while time.monotonic()<deadline:
        path=profile/'recovery-visible.json'
        if path.exists():
            try:
                result=read(path)
            except json.JSONDecodeError:
                time.sleep(.2)
                continue
            assert result['passed'] and result['pid']==window.pid,'Recovery entry did not remain available'
            report['state']='awaiting_human_verification'
            (evidence/'manual-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps({'ready':True,'pid':window.pid,'profile':str(profile)},ensure_ascii=False))
            return
        if window.poll() is not None:
            raise RuntimeError('Manual test window exited before readiness')
        time.sleep(.2)
    raise TimeoutError('Manual test window readiness deadline')


if __name__=='__main__':
    main()
