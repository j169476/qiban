"""Memory-only supervised protocol fixtures; never operate a native window."""
import asyncio
import io
import json
import os
import queue

import pytest

from companion_core.errors import AssistantError
from companion_wechat.contracts import SendRequest, digest
from companion_wechat.live_sender import DriverOutcome
from companion_wechat.supervised_pipe import (
    MAX_LINE,
    PrivateJsonPipe,
    SupervisedPipeOperator,
    require_private_stdio,
)


class Reader:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, _limit):
        return self.lines.get(timeout=5)

    def send(self, value):
        self.lines.put((json.dumps(value, ensure_ascii=False) + "\n").encode())


class Writer:
    def __init__(self):
        self.lines = queue.Queue()

    def write(self, data):
        self.lines.put(data)

    def flush(self):
        pass

    async def next(self):
        async with asyncio.timeout(2):
            while self.lines.empty():
                await asyncio.sleep(0.001)
        return json.loads(self.lines.get_nowait())


@pytest.fixture
async def wire():
    reader, writer = Reader(), Writer()
    channel = PrivateJsonPipe(reader, writer)
    yield reader, writer, channel
    await channel.close()
    reader.lines.put(b"")


async def hello(reader, writer, channel):
    task = asyncio.create_task(channel.handshake())
    frame = await writer.next()
    assert frame["type"] == "ready" and frame["protocol"] == "wechat-supervised-v1"
    reader.send({"v": 1, "type": "ready", "id": frame["id"]})
    await task


def request():
    return SendRequest("synthetic-action", "synthetic-generation", "synthetic-account", "synthetic-source",
                       "owner_main", 1, "synthetic-owner", "synthetic candidate reply")


class Receipts:
    def __init__(self, confirmed=True):
        self.events = []
        self.confirmed = confirmed

    async def begin(self, req):
        self.events.append(("begin", req.action_id))
        return object()

    async def confirm(self, req, _token):
        self.events.append(("confirm", req.action_id))
        return DriverOutcome("confirmed", True) if self.confirmed else DriverOutcome("uncertain")

    async def discard(self, req, _token):
        self.events.append(("discard", req.action_id))


async def stage(reader, writer, operator):
    task = asyncio.create_task(operator.prepare(request()))
    frame = await writer.next()
    assert frame["type"] == "prepare" and frame["request"]["text"] == request().text
    assert frame["text_digest"] == digest(request().text)
    reader.send({"v": 1, "type": "staged", "id": frame["id"], "client_epoch": "synthetic-native-session",
                 "view_token": "synthetic-final-observation", "observed": True,
                 "draft_matches": True, "manual_takeover": False})
    return frame["id"], await task


@pytest.mark.asyncio
async def test_ordered_native_receipt_begin_then_claim_grant_and_database_confirmation(wire):
    reader, writer, channel = wire
    await hello(*wire)
    receipts = Receipts()
    operator = SupervisedPipeOperator(channel, receipts)
    key, proof = await stage(reader, writer, operator)
    assert receipts.events == [("begin", request().action_id)]
    grants = []

    def authorize():
        grants.append(True)
        return {"expires_unix_ms": 123456789}

    task = asyncio.create_task(operator.dispatch_once(request(), proof, authorize=authorize))
    assert await writer.next() == {"v": 1, "type": "armed", "id": key, "view_token": proof.view_token}
    assert not grants
    reader.send({"v": 1, "type": "claim", "id": key, "view_token": proof.view_token})
    assert await writer.next() == {"v": 1, "type": "grant", "id": key, "expires_unix_ms": 123456789}
    assert grants == [True]
    reader.send({"v": 1, "type": "submitted", "id": key})
    result = await task
    assert result.status == "confirmed" and result.client_recorded
    assert [item[0] for item in receipts.events] == ["begin", "confirm", "discard"]
    with pytest.raises(AssistantError):
        await operator.dispatch_once(request(), proof, authorize=authorize)
    assert grants == [True]


@pytest.mark.asyncio
async def test_parent_submitted_is_insufficient_without_actual_database_receipt(wire):
    reader, writer, channel = wire
    await hello(*wire)
    receipts = Receipts(confirmed=False)
    operator = SupervisedPipeOperator(channel, receipts, confirm_seconds=0.001)
    key, proof = await stage(reader, writer, operator)
    task = asyncio.create_task(operator.dispatch_once(request(), proof,
                                                     authorize=lambda: {"expires_unix_ms": 123}))
    await writer.next()
    reader.send({"v": 1, "type": "claim", "id": key, "view_token": proof.view_token})
    await writer.next()
    reader.send({"v": 1, "type": "submitted", "id": key})
    result = await task
    assert result.status == "uncertain" and not result.client_recorded


@pytest.mark.asyncio
async def test_final_authorization_failure_never_outputs_grant(wire):
    reader, writer, channel = wire
    await hello(*wire)
    receipts = Receipts()
    operator = SupervisedPipeOperator(channel, receipts)
    key, proof = await stage(reader, writer, operator)

    def denied():
        raise AssistantError("core_before_send_cancelled", "synthetic")

    task = asyncio.create_task(operator.dispatch_once(request(), proof, authorize=denied))
    await writer.next()
    reader.send({"v": 1, "type": "claim", "id": key, "view_token": proof.view_token})
    with pytest.raises(AssistantError):
        await task
    assert writer.lines.empty()
    assert [item[0] for item in receipts.events] == ["begin", "discard"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("observed", False), ("draft_matches", False),
                                        ("manual_takeover", True), ("client_epoch", "")])
async def test_unverified_staged_response_closes_before_capturing_receipt(wire, field, value):
    reader, writer, channel = wire
    await hello(*wire)
    receipts = Receipts()
    operator = SupervisedPipeOperator(channel, receipts)
    task = asyncio.create_task(operator.prepare(request()))
    frame = await writer.next()
    response = {"v": 1, "type": "staged", "id": frame["id"], "client_epoch": "synthetic-native-session",
                "view_token": "synthetic-observation", "observed": True, "draft_matches": True,
                "manual_takeover": False}
    reader.send({**response, field: value})
    with pytest.raises(AssistantError):
        await task
    assert receipts.events == []
    with pytest.raises(AssistantError):
        channel.check()


@pytest.mark.asyncio
async def test_wrong_observed_view_claim_closes_without_grant(wire):
    reader, writer, channel = wire
    await hello(*wire)
    receipts = Receipts()
    operator = SupervisedPipeOperator(channel, receipts)
    key, proof = await stage(reader, writer, operator)
    grants = []
    task = asyncio.create_task(operator.dispatch_once(request(), proof, authorize=lambda: grants.append(True)))
    await writer.next()
    reader.send({"v": 1, "type": "claim", "id": key, "view_token": "changed-draft"})
    with pytest.raises(AssistantError):
        await task
    assert grants == [] and writer.lines.empty()


@pytest.mark.asyncio
async def test_receipt_read_timeout_is_uncertain_and_bounded(wire):
    reader, writer, channel = wire
    await hello(*wire)
    receipts = Receipts()

    async def stall(*_args):
        await asyncio.Event().wait()

    receipts.confirm = stall
    operator = SupervisedPipeOperator(channel, receipts, confirm_seconds=0.01)
    key, proof = await stage(reader, writer, operator)
    task = asyncio.create_task(operator.dispatch_once(request(), proof,
                                                     authorize=lambda: {"expires_unix_ms": 123}))
    await writer.next()
    reader.send({"v": 1, "type": "claim", "id": key, "view_token": proof.view_token})
    await writer.next()
    reader.send({"v": 1, "type": "submitted", "id": key})
    result = await task
    assert result.status == "uncertain" and not result.client_recorded
    assert receipts.events[-1][0] == "discard"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["wrong_id", "wrong_phase", "extra", "duplicate", "invalid_json", "utf8", "oversize", "partial"])
async def test_invalid_input_is_bounded_sanitized_and_terminal(wire, damage):
    reader, writer, channel = wire
    task = asyncio.create_task(channel.handshake())
    frame = await writer.next()
    response = {"v": 1, "type": "ready", "id": frame["id"]}
    if damage == "wrong_id":
        response["id"] = "f" * 32
    elif damage == "wrong_phase":
        response["type"] = "submitted"
    elif damage == "extra":
        response["private"] = "SYNTHETIC_SECRET"
    if damage == "duplicate":
        raw = ('{"v":1,"v":1,"type":"ready","id":"' + frame["id"] + '"}\n').encode()
    elif damage == "invalid_json":
        raw = b"SYNTHETIC_SECRET\n"
    elif damage == "utf8":
        raw = b"\xff\n"
    elif damage == "oversize":
        raw = b"x" * (MAX_LINE + 1)
    elif damage == "partial":
        raw = json.dumps(response).encode()
    else:
        raw = (json.dumps(response) + "\n").encode()
    reader.lines.put(raw)
    with pytest.raises(AssistantError) as exc:
        await task
    assert exc.value.code in {"wechat_pipe_protocol", "wechat_pipe_limit"}
    assert "SYNTHETIC_SECRET" not in str(exc.value)
    with pytest.raises(AssistantError):
        channel.check()


@pytest.mark.asyncio
async def test_eof_cancels_work_even_while_model_is_awaiting_and_runs_cleanup(wire):
    reader, writer, channel = wire
    await hello(*wire)
    entered, closed = asyncio.Event(), []

    async def model():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    task = asyncio.create_task(channel.supervise(model()))
    await entered.wait()
    reader.lines.put(b"")
    with pytest.raises(AssistantError) as exc:
        await task
    assert exc.value.code == "wechat_pipe_closed" and closed == [True]


@pytest.mark.asyncio
async def test_unsolicited_input_cancels_idle_work(wire):
    reader, writer, channel = wire
    await hello(*wire)
    reader.send({"v": 1, "type": "submitted", "id": "1" * 32})
    async with asyncio.timeout(1):
        await channel._failed.wait()
    with pytest.raises(AssistantError) as exc:
        channel.check()
    assert exc.value.code == "wechat_pipe_protocol"


@pytest.mark.asyncio
async def test_handshake_timeout_has_fixed_error_and_no_retry(wire):
    _reader, _writer, channel = wire
    with pytest.raises(AssistantError) as exc:
        await channel.handshake(timeout=0.01)
    assert exc.value.code == "wechat_pipe_timeout"


def test_stdio_must_be_pipes_and_not_files_or_memory_streams(tmp_path):
    with pytest.raises(AssistantError):
        require_private_stdio(io.BytesIO(), io.BytesIO())
    with (tmp_path / "synthetic.txt").open("w+b") as file, pytest.raises(AssistantError):
        require_private_stdio(file, file)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb") as writer:
        require_private_stdio(reader, writer)


@pytest.mark.asyncio
async def test_close_discards_staged_receipt_once_without_touching_parent_ui(wire):
    reader, writer, channel = wire
    await hello(*wire)
    receipts = Receipts()
    operator = SupervisedPipeOperator(channel, receipts)
    await stage(reader, writer, operator)
    await operator.close()
    await operator.close()
    assert [item[0] for item in receipts.events] == ["begin", "discard"]
