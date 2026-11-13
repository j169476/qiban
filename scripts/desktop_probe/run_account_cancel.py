"""Verify actual managed-login cancellation feedback in a disposable EXE window."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))

from credential_vault import VaultCredentials
from run_management import ProcessScope
from storage_worker import VAULT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    exe,evidence=args.exe.resolve(strict=True),args.evidence.resolve()
    evidence.mkdir(parents=True,exist_ok=False)
    profile=evidence/'取消反馈测试'
    report={'passed':False}
    scope=ProcessScope()
    try:
        with (evidence/'window.log').open('wb') as log:
            child=subprocess.Popen([str(exe),'--data-dir',str(profile),'--model-cancel-smoke'],
                stdout=log,stderr=log,stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW|0x4)
            scope.attach(child)
            scope.resume()
            assert child.wait(timeout=180)==0,'Account cancellation window failed'
        result=json.loads((profile/'result.json').read_text(encoding='utf8'))
        assert result['passed']
        report.update(passed=True,cases=[row['name'] for row in result['events'] if row['name'].startswith('check_')])
    finally:
        scope.close()
        root=profile/'data/installation'
        if (root/'installation.json').exists():
            identity=json.loads((root/'installation.json').read_text())['installation']
            vault=VaultCredentials()
            if vault.get_password(VAULT,identity) is not None:
                vault.delete_password(VAULT,identity)
            report['test_database_credentials_removed']=vault.get_password(VAULT,identity) is None
        (evidence/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))


if __name__=='__main__':
    main()
