"""Exercise uninstall preparation against a fresh synthetic database, without uninstalling the host."""
import argparse
import json
import subprocess
from pathlib import Path

from run_managed_launch import VAULT, ProcessScope, VaultCredentials, read


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    exe,evidence=args.exe.resolve(strict=True),args.evidence.resolve()
    evidence.mkdir(parents=True,exist_ok=False)
    profile=evidence/'中文用户'
    root=profile/'data/installation'
    vault=VaultCredentials()
    report={'passed':False}
    scope=ProcessScope()
    try:
        provision=subprocess.run([str(exe),'--install-host',str(root),str(exe.parent),str(evidence/'initialize'),''],
            stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=180)
        assert provision.returncode==0
        identity=read(root/'installation.json')['installation']
        credential=vault.get_password(VAULT,identity)
        process=subprocess.Popen([str(exe),'--uninstall','--uninstall-smoke','--data-dir',str(profile)],
            stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW|0x4)
        scope.attach(process)
        scope.resume()
        assert process.wait(timeout=180)==0
        result=read(profile/'result.json')
        assert result['passed']
        assert not (root/'database/postmaster.pid').exists()
        assert vault.get_password(VAULT,identity)==credential
        report.update(passed=True,checks=[e['name'] for e in result['events'] if e['name'].startswith('check_')],
                      database_stopped=True,credential_unchanged=True)
    finally:
        scope.close()
        if (root/'installation.json').exists():
            identity=read(root/'installation.json')['installation']
            if vault.get_password(VAULT,identity) is not None:
                vault.delete_password(VAULT,identity)
            report['synthetic_credential_removed']=vault.get_password(VAULT,identity) is None
        (evidence/'uninstall-backup-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))


if __name__=='__main__':
    main()
