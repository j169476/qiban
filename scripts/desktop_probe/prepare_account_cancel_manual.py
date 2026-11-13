"""Leave a pending managed login in the verified manual profile; no browser is opened."""
import argparse
import json
import subprocess
import time
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    exe,evidence=args.exe.resolve(strict=True),args.evidence.resolve(strict=True)
    metadata=evidence/'manual-test.json'
    report=json.loads(metadata.read_text(encoding='utf8'))
    observed=json.loads((evidence/'account-reopen-result.json').read_text(encoding='utf8'))
    assert observed['passed'] and 'logout_persists_after_backend_restart' in observed['cases']
    profile=Path(report['profile']).resolve(strict=True)
    assert profile.parent==evidence
    marker=profile/'model-cancel-visible.json'
    assert not marker.exists(),'Use a fresh cancellation attempt, do not trust stale evidence'
    window=subprocess.Popen([str(exe),'--data-dir',str(profile),'--model-cancel-inspect'],cwd=evidence,
        stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    report.update(state='preparing_login_cancellation',application=str(exe),pid=window.pid,
                  user_confirmed_login=True,login_restart_logout_verified=True)
    def save():
        metadata.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    save()
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        if marker.exists():
            result=json.loads(marker.read_text(encoding='utf8'))
            assert result['passed'] and result['pid']==window.pid,'Pending login controls not ready'
            report.update(state='awaiting_human_login_cancellation',browser_opened=False)
            save()
            print(json.dumps({'ready':True,'pid':window.pid,'browser_opened':False}))
            return
        if window.poll() is not None:
            raise RuntimeError('Cancellation window exited before readiness')
        time.sleep(.25)
    raise TimeoutError('Cancellation control readiness deadline')


if __name__=='__main__':
    main()
