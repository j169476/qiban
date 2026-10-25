import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_wechat.contracts import Connection, SendRequest
from companion_wechat.receipt_client import DatabaseReceiptClient
from companion_wechat.receiver_transport import WorkerError


class Worker:
    def __init__(self):
        self.calls = []
        self.result = {"status": "client_recorded", "confirmed": True}
        self.error = None

    async def request(self, command, **arguments):
        self.calls.append((command, arguments))
        if self.error:
            raise self.error
        if command == "receipt_begin":
            return {"handle": "a" * 64}
        if command == "receipt_discard":
            return {"discarded": True}
        return self.result


@pytest.fixture
def env():
    worker = Worker()
    bindings = SimpleNamespace(get=lambda key: SimpleNamespace(
        enabled=True, revision=1, target_ref="synthetic-owner"))
    receiver = SimpleNamespace(_closed=False, _worker=worker, _lock=asyncio.Lock(), bindings=bindings,
                               connection=lambda: Connection("synthetic-self", "synthetic-inbox-epoch"))

    async def failed(error):
        receiver._worker = None

    receiver._failed = failed
    request = SendRequest("synthetic-action", "synthetic-generation", "synthetic-self", "synthetic-inbox-epoch",
                          "owner_main", 1, "synthetic-owner", "synthetic reply")
    return receiver, worker, request


@pytest.mark.asyncio
async def test_receipt_commands_use_same_worker_and_no_saved_cursor(env):
    receiver, worker, request = env
    client = DatabaseReceiptClient(receiver)
    token = await client.begin(request)
    result = await client.confirm(request, token)
    assert result.status == "confirmed" and result.client_recorded
    assert worker.calls[0] == ("receipt_begin", {"action_id": request.action_id, "target_ref": request.target_ref})
    assert worker.calls[1][1] == {"action_id": request.action_id, "handle": "a" * 64, "text": request.text}
    assert "synthetic" not in repr(token) and "aaaa" not in repr(token)
    await client.discard(request, token)
    assert worker.calls[-1][0] == "receipt_discard"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "ambiguous", "uncertain"])
async def test_absent_evidence_never_turns_into_rejected_or_success(env, status):
    receiver, worker, request = env
    worker.result = {"status": status, "confirmed": False}
    client = DatabaseReceiptClient(receiver)
    assert (await client.confirm(request, await client.begin(request))).status == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"status": "client_recorded", "confirmed": False},
    {"status": "uncertain", "confirmed": True},
    {"status": "client_recorded", "confirmed": True, "private": "synthetic"},
    {"status": "delivered", "confirmed": True},
])
async def test_malformed_reply_is_uncertain(env, result):
    receiver, worker, request = env
    worker.result = result
    client = DatabaseReceiptClient(receiver)
    assert (await client.confirm(request, await client.begin(request))).status == "uncertain"


@pytest.mark.asyncio
async def test_reconnect_never_reuses_old_watermark(env):
    receiver, worker, request = env
    client = DatabaseReceiptClient(receiver)
    token = await client.begin(request)
    receiver._worker = Worker()
    assert (await client.confirm(request, token)).status == "uncertain"
    assert receiver._worker.calls == []


@pytest.mark.asyncio
async def test_other_action_or_client_cannot_reuse_handle(env):
    receiver, worker, request = env
    client = DatabaseReceiptClient(receiver)
    token = await client.begin(request)
    assert (await client.confirm(replace(request, text="different"), token)).status == "uncertain"
    assert (await DatabaseReceiptClient(receiver).confirm(request, token)).status == "uncertain"
    assert len(worker.calls) == 1


@pytest.mark.asyncio
async def test_expired_handle_and_wrong_target_fail_before_read(env):
    receiver, worker, request = env
    now = [0.0]
    client = DatabaseReceiptClient(receiver, clock=lambda: now[0])
    token = await client.begin(request)
    now[0] = 121
    assert (await client.confirm(request, token)).status == "uncertain"
    with pytest.raises(AssistantError):
        await client.begin(replace(request, conversation="other"))
    assert len(worker.calls) == 1


@pytest.mark.asyncio
async def test_dead_worker_preserves_receive_reconnect_path(env):
    receiver, worker, request = env
    client = DatabaseReceiptClient(receiver)
    token = await client.begin(request)
    worker.error = WorkerError("worker_disconnected")
    assert (await client.confirm(request, token)).status == "uncertain"
    assert receiver._worker is None
