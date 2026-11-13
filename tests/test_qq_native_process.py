import io
import json
import threading
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_qq.native_process import NativeProcess


def process_with_reply(payload, *, noise=b"native diagnostic\n", correct_prefix=True):
    worker = NativeProcess({}, timeout=1)
    prefix = worker.prefix if correct_prefix else b"QIBAN_NATIVE:wrong:"
    stdin = io.BytesIO()
    worker.process = SimpleNamespace(stdin=stdin,
        stdout=io.BytesIO(noise + prefix + json.dumps(payload).encode() + b"\n"), poll=lambda: None)
    return worker, stdin


def test_accept_only_private_framed_response_and_ignore_native_diagnostics():
    worker, stdin = process_with_reply({"id": 1, "ok": True, "result": {"groups": []}})
    assert worker.request("identity") == {"groups": []}
    sent = json.loads(stdin.getvalue())
    assert sent == {"id": 1, "command": "identity", "args": {}}
    worker.close()
    assert worker.closed


@pytest.mark.parametrize("payload", [
    {"id": 2, "ok": True, "result": {}},
    {"id": 1, "ok": True, "result": {}, "extra": "private"},
    {"id": 1, "ok": False, "error": "PRIVATE-RAW-ERROR"},
    {"id": 1, "ok": 1, "result": {}},
    {"id": True, "ok": True, "result": {}},
    {"id": 1, "ok": False, "error": {"private": "invalid"}},
])
def test_invalid_reply_closes_channel_without_exposing_native_error(payload):
    worker, _ = process_with_reply(payload)
    with pytest.raises(AssistantError) as error:
        worker.request("identity")
    assert error.value.code == "qq_native_protocol_invalid"
    assert "PRIVATE" not in str(error.value) and worker.closed


def test_other_process_prefix_cannot_become_a_response():
    worker, _ = process_with_reply({"id": 1, "ok": True, "result": {}}, correct_prefix=False)
    with pytest.raises(AssistantError):
        worker.request("identity")
    assert worker.closed


def test_login_requirement_is_explicit_not_an_empty_catalog():
    worker, _ = process_with_reply({"id": 1, "ok": False, "error": "qq_login_required"})
    with pytest.raises(AssistantError) as error:
        worker.request("open")
    assert error.value.code == "qq_login_required"
    worker.close()


@pytest.mark.parametrize(("wire", "reason"), [
    (b"", "pipe_eof"),
    (b"PREFIX{private-invalid-json\n", "invalid_json"),
    (b'PREFIX{"id":999,"ok":true,"result":"private"}\n', "reply_identity"),
    (b'PREFIX{"id":1,"ok":true,"result":"private","extra":1}\n', "reply_shape"),
])
def test_failure_evidence_is_bounded_metadata_without_received_content(wire, reason):
    worker, _ = process_with_reply({})
    worker.process.stdout = io.BytesIO(wire.replace(b"PREFIX", worker.prefix))
    worker.process.poll = lambda: 73 if worker.process.stdout.closed else None
    with pytest.raises(AssistantError):
        worker.request("identity")
    assert worker.last_failure["reason"] == reason
    assert worker.last_failure["request_id"] == 1
    assert set(worker.last_failure) == {
        "reason", "request_id", "discarded_bytes", "max_line_bytes", "exit_code_before_cleanup"
    }
    assert "private" not in json.dumps(worker.last_failure)


def test_stop_interrupts_a_waiting_request_without_waiting_for_its_timeout():
    worker, _ = process_with_reply({})
    worker.timeout = 10
    reading, released = threading.Event(), threading.Event()

    class BlockingPipe:
        def readline(self, limit):
            reading.set()
            assert released.wait(5), "test cleanup must release the waiting reader"
            return b""

        def close(self):
            released.set()

    worker.process.stdout = BlockingPipe()
    worker.scope = SimpleNamespace(close=released.set)
    errors = []

    def request():
        try:
            worker.request("identity")
        except AssistantError as error:
            errors.append(error.code)

    request_thread = threading.Thread(target=request, daemon=True)
    stop_thread = threading.Thread(target=worker.close, daemon=True)
    try:
        request_thread.start()
        assert reading.wait(1)
        stop_thread.start()
        stop_thread.join(1)
        assert not stop_thread.is_alive(), "stop blocked behind the request lock"
        request_thread.join(1)
        assert not request_thread.is_alive() and errors
        assert worker.last_failure["reason"] == "cancelled"
        assert worker.closed
    finally:
        released.set()
        request_thread.join(2)
        if stop_thread.ident is not None:
            stop_thread.join(2)


def test_dead_worker_exit_code_is_captured_before_owned_cleanup():
    worker, _ = process_with_reply({})
    worker.process.stdout = io.BytesIO()
    observed = iter([None, 73])
    worker.process.poll = lambda: next(observed, 1)
    with pytest.raises(AssistantError):
        worker.request("identity")
    assert worker.last_failure["exit_code_before_cleanup"] == 73


def test_closed_worker_cannot_be_reopened_even_without_runtime_configuration():
    worker = NativeProcess({})
    worker.close()
    with pytest.raises(AssistantError) as error:
        worker.open()
    assert error.value.code == "qq_native_process_state"
