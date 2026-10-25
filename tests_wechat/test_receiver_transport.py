"""Real short-lived synthetic children exercise pipe deadlines and reaping."""
import asyncio
import subprocess
import sys

import pytest

from companion_wechat.receiver_transport import SubprocessTransport, WorkerError


def synthetic_process(monkeypatch, code):
    original = subprocess.Popen
    processes = []

    def spawn(arguments, **options):
        assert arguments[-2:] == ["-m", "companion_wechat.db_worker"]
        assert options["stderr"] == subprocess.DEVNULL and not options.get("shell")
        process = original([sys.executable, "-u", "-c", code], **options)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    return processes


@pytest.mark.asyncio
async def test_private_pipe_roundtrip_no_payload_in_arguments_and_reaped(monkeypatch):
    children = synthetic_process(monkeypatch, '''
import sys,json
for line in sys.stdin:
    value=json.loads(line)
    print(json.dumps({'id':value['id'],'ok':True,'result':{'synthetic':value['payload']}}),flush=True)
''')
    transport = SubprocessTransport()
    assert await transport.request("snapshot", payload="synthetic private") == {"synthetic": "synthetic private"}
    assert len(children) == 1 and children[0].poll() is None
    await transport.close()
    assert children[0].poll() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("output,code", [
    ("print('synthetic private raw failure',flush=True)", "worker_protocol_invalid"),
    ("print('{\"id\":999,\"ok\":true,\"result\":{}}',flush=True)", "worker_protocol_invalid"),
    ("print('{\"id\":1,\"ok\":false,\"error\":\"synthetic private raw failure\"}',flush=True)",
     "worker_protocol_invalid"),
    ("print('{\"id\":1,\"ok\":false,\"error\":\"client_not_running\",\"recoverable\":false}',flush=True)",
     "client_not_running"),
    ("print('{\"id\":1,\"ok\":false,\"error\":\"receipt_source_pending\",\"recoverable\":true}',flush=True)",
     "receipt_source_pending"),
    ("print('x'*3000000,flush=True)", "worker_protocol_invalid"),
    ("raise SystemExit(2)", "worker_disconnected"),
])
async def test_untrusted_worker_output_is_bounded_sanitized_and_child_reaped(monkeypatch, output, code):
    children = synthetic_process(monkeypatch, "import sys\nsys.stdin.readline()\n" + output)
    transport = SubprocessTransport(read_timeout=5)
    with pytest.raises(WorkerError) as error:
        await transport.request("snapshot")
    assert error.value.code == code and "synthetic private" not in str(error.value)
    assert children[0].poll() is not None
    assert error.value.recoverable == (code in {"client_not_running", "worker_disconnected"})
    await transport.close()


@pytest.mark.asyncio
async def test_deadline_kills_nonresponsive_child(monkeypatch):
    children = synthetic_process(monkeypatch, "import time; time.sleep(30)")
    transport = SubprocessTransport(read_timeout=0.15)
    with pytest.raises(WorkerError) as error:
        await transport.request("snapshot")
    assert error.value.code == "worker_timeout"
    assert children[0].poll() is not None
    await transport.close()


@pytest.mark.asyncio
async def test_cancel_inflight_request_reaps_worker(monkeypatch):
    children = synthetic_process(monkeypatch, "import time; time.sleep(30)")
    transport = SubprocessTransport(read_timeout=5)
    task = asyncio.create_task(transport.request("snapshot"))
    for _ in range(100):
        if children:
            break
        await asyncio.sleep(0.01)
    assert children
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].poll() is not None
    await transport.close()


@pytest.mark.asyncio
async def test_closed_transport_never_spawns(monkeypatch):
    children = synthetic_process(monkeypatch, "raise SystemExit(0)")
    transport = SubprocessTransport()
    await transport.close()
    with pytest.raises(WorkerError):
        await transport.request("snapshot")
    assert children == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [object(), "x" * 300000], ids=["not-json", "oversized"])
async def test_invalid_request_never_starts_child(monkeypatch, payload):
    children = synthetic_process(monkeypatch, "import time; time.sleep(30)")
    transport = SubprocessTransport()
    with pytest.raises(WorkerError) as error:
        await transport.request("snapshot", payload=payload)
    assert error.value.code == "worker_protocol_invalid"
    assert children == []
