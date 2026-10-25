"""Real local child shutdown with the controlling stdin still connected."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows synchronous pipe reader cleanup')
def test_successful_close_does_not_leave_a_native_reader_at_interpreter_exit():
    source = '''import asyncio,sys
from companion_wechat.supervised_pipe import PrivateJsonPipe
async def main():
 p=PrivateJsonPipe(sys.stdin.buffer,sys.stdout.buffer)
 p.start()
 p.publish('result',{'ok':True})
 await p.close()
asyncio.run(main())
'''
    process = subprocess.Popen([sys.executable, '-u', '-c', source], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        assert json.loads(process.stdout.readline(1024)) == {'v': 1, 'type': 'result', 'ok': True}
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
