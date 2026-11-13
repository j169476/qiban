"""Open a disposable, signed-out application profile for human ChatGPT login."""
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
    exe,evidence=args.exe.resolve(strict=True),args.evidence.resolve()
    evidence.mkdir(parents=True,exist_ok=False)
    profile=evidence/'人工登录资料'
    window=subprocess.Popen([str(exe),'--data-dir',str(profile),'--model-inspect'],cwd=evidence,
        stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    report={'state':'preparing','application':str(exe),'profile':str(profile),'pid':window.pid,
            'model_calls_executed':False,'platform_messages_sent':False}
    def save():
        (evidence/'manual-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    save()
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        path=profile/'model-visible.json'
        if path.exists():
            result=json.loads(path.read_text(encoding='utf8'))
            assert result['passed'] and result['pid']==window.pid,'Model account page not ready'
            root=profile/'data/installation'
            report.update(state='awaiting_human_login',installation=json.loads((root/'installation.json').read_text())['installation'])
            save()
            print(json.dumps({'ready':True,'pid':window.pid,'profile':str(profile)},ensure_ascii=False))
            return
        if window.poll() is not None:
            raise RuntimeError('Application exited before account page readiness')
        time.sleep(.25)
    raise TimeoutError('Account page readiness deadline')


if __name__=='__main__':
    main()
