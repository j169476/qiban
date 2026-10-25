"""Tool-session controller. The actual role child keeps private anonymous pipes.

Only a candidate's alias/text and public protocol states are projected to the
controlling tool. Account IDs and the full request stay in this parent memory.
No file cache, network listener, model implementation or desktop action exists.
"""
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from companion_wechat.supervised_signal import ControlSignal

sys.stdout.reconfigure(encoding='utf-8')
events = queue.Queue(maxsize=64)
signal = ControlSignal(Path('.dev/wechat/ui-signals'))
ui_registered = False
staged = {}
pending_ui = None
process = subprocess.Popen(
    [sys.executable, '-u', '-m', 'companion_wechat.supervised_cli', *sys.argv[1:]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
)


def child_output():
    try:
        while line := process.stdout.readline(16385):
            if len(line) > 16384 or not line.endswith(b'\n'):
                raise ValueError()
            frame = json.loads(line)
            if frame.get('v') != 1:
                raise ValueError()
            if frame.get('type') == 'prepare':
                request = frame['request']
                if request['conversation'] not in ('owner_main', 'family'):
                    raise ValueError()
                frame = {'v': 1, 'type': 'prepare', 'id': frame['id'],
                         'alias': request['conversation'], 'text': request['text'],
                         'text_digest': frame['text_digest']}
            elif frame.get('type') not in ('ready', 'status', 'result', 'armed', 'grant'):
                raise ValueError()
            events.put(('output', frame), timeout=1)
    except Exception:
        events.put(('stop', None), timeout=1)


def parent_input():
    pending = b''
    total = 0
    try:
        while True:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                break
            pending += chunk
            total += len(chunk)
            if len(pending) > 16384 or total > 262144:
                break
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                frame = json.loads(line)
                if (frame.get('v') != 1 or frame.get('type') not in
                        ('ready', 'staged', 'claim', 'submitted', 'ui_key')):
                    raise ValueError()
                events.put(('input', frame), timeout=1)
    except Exception:
        pass
    events.put(('stop', None), timeout=1)


for target in (child_output, parent_input):
    threading.Thread(target=target, daemon=True).start()
closing = False


def publish(frame):
    # Fixed-width ASCII frames survive ConPTY line wrapping without altering
    # Unicode draft text or the very short final-grant deadline.
    encoded = json.dumps(frame, ensure_ascii=False, separators=(',', ':')).encode('utf-8').hex()
    for offset in range(0, len(encoded), 64):
        print('XLDHEX:' + encoded[offset:offset + 64])
    print('XLDEND', flush=True)


def forward(value):
    process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
    process.stdin.flush()


def drive_ui():
    global pending_ui
    if not ui_registered or pending_ui is None or closing:
        return
    action, token, phase = pending_ui
    if phase == 'armed' and signal.receive(action, token, 'ready'):
        forward({'v': 1, 'type': 'claim', 'id': action, 'view_token': token})
        pending_ui = (action, token, 'claimed')
    elif phase == 'grant' and signal.receive(action, token, 'submitted'):
        forward({'v': 1, 'type': 'submitted', 'id': action})
        pending_ui = None


publish({'v': 1, 'type': 'ui_bridge', **signal.public()})


try:
    while process.poll() is None or not events.empty():
        try:
            kind, value = events.get(timeout=0.01)
        except queue.Empty:
            drive_ui()
            continue
        if kind == 'output':
            if ui_registered and value['type'] == 'armed':
                if staged.get(value['id']) != value['view_token']:
                    raise ValueError('unbound_ui')
                pending_ui = (value['id'], value['view_token'], 'armed')
            elif ui_registered and value['type'] == 'grant':
                if pending_ui != (value['id'], staged[value['id']], 'claimed'):
                    raise ValueError('unexpected_ui_grant')
                signal.grant(value['id'], staged[value['id']], value['expires_unix_ms'])
                pending_ui = (value['id'], staged[value['id']], 'grant')
            publish(value)
        elif kind == 'input' and not closing:
            if value['type'] == 'ui_key':
                if set(value) != {'v', 'type', 'public_key'}:
                    raise ValueError('ui_registration')
                signal.register(value['public_key'])
                ui_registered = True
            else:
                if ui_registered and value['type'] in ('claim', 'submitted'):
                    raise ValueError('duplicate_ui_authority')
                if value['type'] == 'staged':
                    staged[value['id']] = value['view_token']
                forward(value)
        elif kind == 'stop' and not closing:
            closing = True
            process.stdin.close()
        drive_ui()
finally:
    if not closing:
        process.stdin.close()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        # Terminate only this child and its descendants after graceful EOF fails.
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
    process.stdout.close()
    signal.close()
publish({'v': 1, 'type': 'exited', 'code': process.returncode})
raise SystemExit(process.returncode or 0)
