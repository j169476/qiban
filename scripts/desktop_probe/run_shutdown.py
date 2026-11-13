"""Check durable shutdown markers and the real native wait/force window, without accounts."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from companion_core.process_scope import ProcessScope


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',required=True,type=Path)
    parser.add_argument('--evidence',required=True,type=Path)
    args=parser.parse_args()
    exe=args.exe.resolve(strict=True)
    evidence=args.evidence.resolve()
    evidence.mkdir(parents=True,exist_ok=False)
    result={'passed':False}
    try:
        for name,arguments,expected in [('journal',['--shutdown-tests',str(evidence/'journal')],0),
                ('window',['--shutdown-smoke','--data-dir',str(evidence/'window')],1)]:
            scope=ProcessScope()
            try:
                process=subprocess.Popen([str(exe),*arguments],stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW|0x4)
                scope.attach(process)
                scope.resume()
                assert process.wait(timeout=60)==expected,name
            finally:
                scope.close()
        journal=json.loads((evidence/'journal/shutdown-result.json').read_text(encoding='utf8'))
        assert journal['passed']
        result['journal_cases']=journal['cases']
        events=[json.loads(line) for line in (evidence/'window/logs/events.jsonl').read_text(encoding='utf8').splitlines()]
        names=[e['name'] for e in events]
        assert 'check_shutdown_wait_preserves_process_pass' in names
        assert names.index('shutdown_waiting_for_user')<names.index('shutdown_force_requested')<names.index('shutdown_completed')
        assert next(e for e in events if e['name']=='shutdown_completed')['detail']['detail']=='forced_stop'
        marker=json.loads((evidence/'window/data/application-session.json').read_text(encoding='utf8'))
        assert marker['Stage']=='forced'
        result['wait_then_explicit_force_and_durable_marker']=True
        result['passed']=True
    finally:
        (evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':
    main()
