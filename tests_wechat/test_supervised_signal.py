"""Real Python/Node signed handoff, with a synthetic UI; no model or WeChat."""
import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from companion_wechat.supervised_signal import ControlSignal


@pytest.mark.parametrize('change', [None, 'tamper', 'expired', 'target'])
def test_signed_grant_crosses_processes_without_exporting_private_keys(tmp_path, change):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    signal = ControlSignal(tmp_path)
    action, token = 'a' * 32, 'synthetic-observed-view'
    module = Path(__file__).resolve().parents[1] / 'scripts/wechat_supervised_ui.mjs'
    script = '''
import {pathToFileURL} from 'node:url';
const [root,key,id,viewToken,module]=process.argv.slice(1);
const {createController}=await import(pathToFileURL(module).href);
const controller=createController();
console.log(JSON.stringify({public_key:controller.publicKey}));
let count=0;
const sky={press_key:async()=>{count++},get_window_state:async()=>({synthetic:true})};
try {await controller.waitAndSubmit({sky,window:{},root,publicKey:key,id,viewToken});
 try {await controller.waitAndSubmit({sky,window:{},root,publicKey:key,id,viewToken});throw Error('replayed');}
 catch(e){if(e.message!=='action_already_consumed')throw e;}
 console.log(JSON.stringify({ok:true,count}));}
catch(e){console.log(JSON.stringify({ok:false,count,error:e.message}));}
'''
    process = subprocess.Popen([node, '--input-type=module', '-e', script, str(signal.root),
        signal.public()['public_key'], action, token, str(module)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        signal.register(json.loads(process.stdout.readline())['public_key'])
        deadline = time.monotonic() + 5
        while not signal.receive(action, token, 'ready'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert not signal.receive(action, token, 'ready')
        if change is None:
            signal.grant(action, token, int(time.time() * 1000) + 1900)
        else:
            # Authenticated but invalid fixtures exercise the JS deadline/scope
            # checks independently of the Python producer's own validation.
            value = {'v': 1, 'type': 'grant', 'id': action, 'view_token': token,
                     'expires_unix_ms': int(time.time() * 1000) + 1900}
            if change == 'expired':
                value['expires_unix_ms'] -= 3000
            if change == 'target':
                value['view_token'] = 'other-view'
            body = json.dumps(value).encode()
            signature = signal._key.sign(body)
            if change == 'tamper':
                body += b' '
            signal._path(action, 'grant').write_text(json.dumps({
                'payload': base64.b64encode(body).decode(),
                'signature': base64.b64encode(signature).decode()}))
        result = json.loads(process.stdout.readline())
        assert process.wait(timeout=5) == 0
        assert result['ok'] == (change is None)
        assert result['count'] == int(change is None)
        assert signal.receive(action, token, 'submitted') == (change is None)
        assert not signal.receive(action, token, 'submitted')
        for file in signal.root.iterdir():
            assert 'PRIVATE KEY' not in file.read_text()
        if change is None:
            with pytest.raises(ValueError):
                signal.grant(action, token, int(time.time() * 1000) + 1900)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        signal.close()
    assert not signal.root.exists()
