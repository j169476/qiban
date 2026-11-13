import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_qq.native_process import NativeProcess
from companion_qq.native_staging import NativeStaging, exited


def test_recovery_only_removes_proven_dead_owned_copies(tmp_path):
    dead = NativeStaging(tmp_path, 'a' * 64)
    live = NativeStaging(tmp_path, 'b' * 64)
    dead.create(11)
    live.create(22)
    (dead.path / ('c' * 32 + '.part')).write_bytes(b'interrupted')
    (dead.path / ('d' * 32 + '.bin')).write_bytes(b'complete')
    legacy = dead.root / 'old-probe.bin'
    legacy.write_bytes(b'leave')
    cache = tmp_path / 'accounts'
    cache.mkdir()
    (cache / 'sdk-cache').write_bytes(b'leave')
    assert live.recover(is_exited=lambda pid: pid == 11) == 1
    assert not dead.path.exists() and live.path.exists()
    assert legacy.read_bytes() == b'leave'
    assert (cache / 'sdk-cache').read_bytes() == b'leave'
    assert live.recover(is_exited=lambda pid: False) == 0


@pytest.mark.parametrize('unexpected', ['user.txt', 'nested'])
def test_unknown_contents_reject_cleanup_before_any_deletion(tmp_path, unexpected):
    stage = NativeStaging(tmp_path, 'a' * 64)
    stage.create(11)
    owned = stage.path / ('c' * 32 + '.bin')
    owned.write_bytes(b'preserved')
    extra = stage.path / unexpected
    if unexpected == 'nested':
        extra.mkdir()
    else:
        extra.write_text('unknown')
    with pytest.raises(AssistantError, match='暂存目录'):
        stage.recover(is_exited=lambda pid: True)
    assert owned.read_bytes() == b'preserved' and (stage.path / 'owner.json').exists()


@pytest.mark.parametrize('marker', [None, '{', '{}', '{"format":1,"worker_pid":true}'])
def test_missing_or_invalid_owner_is_never_guessed(tmp_path, marker):
    stage = NativeStaging(tmp_path, 'a' * 64)
    stage.path.mkdir()
    if marker is not None:
        (stage.path / 'owner.json').write_text(marker)
    assert stage.recover(is_exited=lambda pid: pytest.fail('must not check unknown owner')) == 0
    assert stage.path.exists()


def test_real_worker_death_recovery_and_current_process_protection(tmp_path):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stage = NativeStaging(tmp_path, 'a' * 64)
    try:
        stage.create(child.pid)
        (stage.path / ('b' * 32 + '.part')).write_bytes(b'partial')
        assert not exited(os.getpid()) and not exited(child.pid)
        assert stage.recover() == 0 and stage.path.exists()
        child.kill()
        child.wait(timeout=5)
        assert exited(child.pid)
        assert stage.recover() == 1 and not stage.path.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_close_only_cleans_after_owned_writer_stopped(tmp_path):
    stage = NativeStaging(tmp_path, 'a' * 64)
    stage.create(11)
    (stage.path / ('b' * 32 + '.part')).write_bytes(b'partial')
    worker = NativeProcess({})
    worker.media_stage = stage
    worker.process = SimpleNamespace(stdin=None, stdout=None, poll=lambda: None)
    worker.close()
    assert stage.path.exists()
    worker.process.poll = lambda: 1
    worker.close()
    assert not stage.path.exists()
    worker.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job owner interruption')
def test_host_crash_kills_writer_then_next_start_cleans_partial(tmp_path):
    helper = r'''
import json, subprocess, sys
from companion_core.process_scope import ProcessScope
from companion_qq.native_staging import NativeStaging
stage = NativeStaging(sys.argv[1], 'a' * 64)
writer_code = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.write_bytes(b'partial'); print('ready',flush=True); time.sleep(60)"
scope = ProcessScope()
writer = subprocess.Popen([sys.executable, '-c', writer_code, str(stage.path / ('b' * 32 + '.part'))],
    stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW | 4)
scope.attach(writer)
stage.create(writer.pid)
scope.resume()
assert writer.stdout.readline().strip() == 'ready'
print(json.dumps({'writer':writer.pid}),flush=True)
sys.stdin.read()
scope.close()
'''
    owner = subprocess.Popen([sys.executable, '-c', helper, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        # The helper has a bounded writer and no account or network dependency.
        import queue
        import threading
        import time

        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(owner.stdout.readline()), daemon=True).start()
        writer = json.loads(ready.get(timeout=10))['writer']
        stage = NativeStaging(tmp_path, 'a' * 64)
        assert not exited(writer) and stage.recover() == 0
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not exited(writer) and time.monotonic() < deadline:
            time.sleep(.02)
        assert exited(writer)
        assert stage.recover() == 1 and not stage.path.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        for pipe in (owner.stdin, owner.stdout, owner.stderr):
            pipe.close()


@pytest.mark.parametrize('extension', ['png', 'jpg', 'gif', 'webp'])
def test_outgoing_image_copies_recovered_only_after_owner_exit(tmp_path, extension):
    stage = NativeStaging(tmp_path, 'a' * 64)
    stage.create(11)
    content = stage.path / ('b' * 32 + '.' + extension)
    content.write_bytes(b'owned outgoing image')
    assert stage.recover(is_exited=lambda pid: False) == 0 and content.exists()
    assert stage.recover(is_exited=lambda pid: True) == 1 and not stage.path.exists()
