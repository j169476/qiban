"""Offline direct-web adapter regressions; no model, token or account is contacted."""

import asyncio
import gzip
import json
from types import SimpleNamespace

import httpx
import pytest

from companion_codex import plan_web
from companion_codex.task_tools import RESULT, checked_web
from companion_core.errors import AssistantError

TOKEN = "synthetic-access-token-157-only"
URL = "https://docs.python.org/3/library/asyncio-task.html"
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
OPTIONAL = {
    "type": "object",
    "properties": {"value": {"type": "integer"}, "note": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}
USAGE = {"input_tokens": 12, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 4},
         "output_tokens_details": {"reasoning_tokens": 2}}


def sse(*events):
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode()


def open_item(key="ws_open1", url=URL, status="completed", kind="open_page"):
    return {"id": key, "type": "web_search_call", "status": status,
            "action": {"type": kind, "url": url}}


def message(text, key="msg_1"):
    return {"id": key, "type": "message", "status": "completed",
            "content": [{"type": "output_text", "text": text}]}


def done(item):
    return {"type": "response.output_item.done", "item": item}


def added(item):
    return {"type": "response.output_item.added", "item": item}


def completed(output=(), *, model="gpt-6.1-terra", response_id="resp_1", usage=USAGE):
    return {"type": "response.completed", "response": {
        "id": response_id, "model": model, "output": list(output), "usage": usage}}


def web_answer(url=URL):
    return json.dumps({"status": "done", "reason": "", "image_path": "", "sources": [{
        "url": url, "title": "Python 文档", "excerpt": "cancel 只请求取消。",
        "note": "依据官方文档。", "tags": []}]}, ensure_ascii=False)


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, silent=False):
        self.chunks, self.silent, self.closed = list(chunks), silent, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.silent:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class Transport:
    """MockTransport double: records requests and keeps its streams observable."""

    def __init__(self, events=(), *, status=200, body=None, silent=False, silent_headers=False,
                 chunks=None, silent_first=False, compress=False):
        self.requests, self.streams = [], []
        self.events, self.status, self.body = list(events), status, body
        self.silent, self.silent_headers, self.chunks = silent, silent_headers, chunks
        self.silent_first, self.compress = silent_first, compress
        self.cancelled = False
        self.transport = httpx.MockTransport(self.stall if silent_headers else self.handle)

    def headers(self, kind):
        value = {"content-type": kind}
        if self.compress:
            value["content-encoding"] = "gzip"
        return value

    def handle(self, request):
        self.requests.append({"method": request.method, "url": str(request.url),
                              "authorization": request.headers.get("authorization"),
                              "body": json.loads(request.content)})
        if self.status != 200:
            payload = json.dumps(self.body or {}).encode()
            if self.compress:
                payload = gzip.compress(payload)
            return httpx.Response(self.status, content=payload,
                                  headers=self.headers("application/json"))
        payload = sse(*self.events) if self.chunks is None else self.chunks
        parts = list(payload) if isinstance(payload, (list, tuple)) else [payload]
        if self.compress:
            parts = [gzip.compress(b"".join(parts))]
        quiet = self.silent and (not self.silent_first or not self.streams)
        # The first request must remain in flight, not emit completion before
        # the account-switch test reaches its cancellation boundary.
        stream = Stream([] if quiet and self.silent_first else parts, silent=quiet)
        self.streams.append(stream)
        return httpx.Response(200, headers=self.headers("text/event-stream"), stream=stream)

    async def stall(self, request):
        self.requests.append({"method": request.method, "url": str(request.url),
                              "authorization": request.headers.get("authorization"),
                              "body": json.loads(request.content)})
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


class Tokens:
    def __init__(self):
        self.observed = []

    def tokens(self, usage):
        self.observed.append(dict(usage))


async def run(transport, observation=None, *, schema=SCHEMA, images=(), model="gpt-6.1-terra",
              guard=lambda: None, policy=None, token=TOKEN, phase=lambda value: None):
    return await plan_web.generate(
        "Return JSON.", {"question": "synthetic"}, schema, images,
        policy=policy if policy is not None else {"worker_timeout_seconds": 3},
        guard=guard, phase=phase, observation=observation or Tokens(),
        access=SimpleNamespace(account_id="account-a", access_token=token),
        model=model, reasoning_effort="low", transport=transport.transport)


async def test_request_is_fixed_and_never_carries_the_token():
    transport = Transport([done(message(json.dumps({"value": 42}))), completed()])
    await run(transport, Tokens())
    (request,) = transport.requests
    assert request["method"] == "POST" and request["url"] == plan_web.ENDPOINT
    assert request["authorization"] == "Bearer " + TOKEN
    body = request["body"]
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "gpt-6.1-terra" and body["reasoning"] == {"effort": "low"}
    assert body["tools"] == [{"type": "web_search", "external_web_access": True}]
    assert body["include"] == ["web_search_call.action.sources"]
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert set(body["text"]["format"]["schema"]["required"]) == {"value"}
    assert isinstance(body["input"], list) and body["input"][0]["role"] == "user"
    for forbidden in ("max_output_tokens", "max_tool_calls", "background",
                      "previous_response_id", "OPENAI_API_KEY", "api_key"):
        assert forbidden not in body
    assert TOKEN not in json.dumps(body)


async def test_empty_terminal_keeps_open_items_and_passes_checked_web():
    transport = Transport([done(open_item()), done(message(web_answer())), completed([])])
    observation = Tokens()
    response = await run(transport, observation, schema=RESULT)
    assert response["evidence"]["opened"] == [{"url": URL, "reference": "ws_open1"}]
    assert response["evidence"]["web_calls"] == 1
    assert response["evidence"]["thread_id"] == "resp_1"
    assert response["evidence"]["started"] > 0
    [source] = checked_web(response)
    assert source["url"] == URL and source["reference"] == "ws_open1"
    assert observation.observed[-1]["input_tokens"] == 12
    assert observation.observed[-1]["cached_input_tokens"] == 4
    assert observation.observed[-1]["reasoning_output_tokens"] == 2
    assert observation.observed[-1]["usage_events"] == 1


async def test_terminal_output_cannot_erase_observed_open_evidence():
    transport = Transport([
        done(open_item()), added(open_item()), done(message(web_answer())),
        completed([{"id": "ws_open1", "type": "web_search_call"}, {"id": "msg_1", "type": "message"}]),
    ])
    response = await run(transport, Tokens(), schema=RESULT)
    assert response["evidence"]["opened"] == [{"url": URL, "reference": "ws_open1"}]
    assert response["evidence"]["web_calls"] == 1
    assert response["result"]["status"] == "done"
    assert checked_web(response)[0]["reference"] == "ws_open1"


async def test_search_summary_or_citation_cannot_replace_an_opened_page():
    transport = Transport([
        done(open_item(kind="search")),
        done({"id": "msg_1", "type": "message", "status": "completed", "content": [{
            "type": "output_text", "text": web_answer(),
            "annotations": [{"type": "url_citation", "url": URL, "title": "Python"}]}]}),
        completed([]),
    ])
    transport.events[0]["item"]["action"] = {"type": "search", "query": "asyncio cancel",
                                             "sources": [{"url": URL}]}
    response = await run(transport, Tokens(), schema=RESULT)
    assert response["evidence"]["opened"] == []
    assert response["evidence"]["web_calls"] == 1
    with pytest.raises(AssistantError) as caught:
        checked_web(response)
    assert caught.value.code == "native_web_evidence"


@pytest.mark.parametrize("kind", ["function_call", "command_execution", "custom_tool_call",
                                  "image_generation_call"])
async def test_unrelated_tool_items_stop_the_request(kind):
    transport = Transport([done({"id": "x1", "type": kind, "status": "completed"}), completed()])
    with pytest.raises(AssistantError) as caught:
        await run(transport, Tokens())
    assert caught.value.code == "model_tool_denied"


async def test_web_tool_calls_are_capped_at_six():
    items = [done(open_item(key=f"ws_{index}")) for index in range(plan_web.MAX_WEB_CALLS)]
    transport = Transport([*items, done(message(json.dumps({"value": 42}))), completed()])
    assert (await run(transport, Tokens()))["evidence"]["web_calls"] == plan_web.MAX_WEB_CALLS
    over = Transport([*items, done(open_item(key="ws_7")), completed()])
    with pytest.raises(AssistantError) as caught:
        await run(over, Tokens())
    assert caught.value.code == "native_tool_limit"


@pytest.mark.parametrize("code", ["subscription_sharing_usage_limit_exceeded",
                                  "subscription_sharing_usage_unavailable"])
async def test_subscription_sharing_usage_failure_is_structured(code):
    transport = Transport(status=429, body={"error": {"code": code, "type": "invalid_request_error",
                                                       "message": "leaks " + TOKEN}})
    with pytest.raises(AssistantError) as caught:
        await run(transport, Tokens())
    assert caught.value.code == "model_usage_limit"
    assert caught.value.diagnostics == {"http_status": 429, "server_code": "model_usage_limit"}
    assert TOKEN not in str(caught.value) and TOKEN not in json.dumps(caught.value.diagnostics)


@pytest.mark.parametrize("status,code", [(400, "model_request_invalid"), (401, "model_auth"),
                                         (403, "model_permission"), (429, "model_rate_limit"),
                                         (503, "model_temporary")])
async def test_http_status_maps_to_a_public_failure(status, code):
    transport = Transport(status=status, body={"error": {"message": "leaks " + TOKEN}})
    with pytest.raises(AssistantError) as caught:
        await run(transport, Tokens())
    assert caught.value.code == code
    assert caught.value.diagnostics == {"http_status": status}
    assert TOKEN not in str(caught.value)


async def test_usage_is_kept_when_the_stream_fails():
    failed = {"type": "response.failed", "response": {
        "id": "resp_2", "model": "gpt-6.1-terra", "output": [], "usage": USAGE,
        "error": {"code": "server_error", "message": "leaks " + TOKEN}}}
    observation = Tokens()
    transport = Transport([failed])
    with pytest.raises(AssistantError) as caught:
        await run(transport, observation)
    assert caught.value.code == "model_temporary"
    assert observation.observed[-1]["input_tokens"] == 12
    assert observation.observed[-1]["usage_events"] == 1
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("events,code", [
    ([done(message(json.dumps({"value": 42}))),
      {"type": "response.incomplete", "response": {"model": "gpt-6.1-terra", "output": []}}],
     "model_interrupted"),
    ([done(message(json.dumps({"value": 42}))),
      {"type": "error", "code": "server_error", "message": "leaks " + TOKEN}], "model_temporary"),
    ([done(message("invalid-json")), completed()], "model_output_invalid"),
    ([done(message(json.dumps({"value": "text"}))), completed()], "model_output_invalid"),
    ([done(message(json.dumps({"value": 42}))), completed(model="gpt-6.1-terra-mini")],
     "model_unavailable"),
    ([done(message(json.dumps({"value": 42})))], "model_interrupted"),
    ([{"type": "response.output_item.done", "item": "not-a-dict"}], "model_protocol"),
])
async def test_incomplete_or_invalid_streams_never_return(events, code):
    transport = Transport(events)
    with pytest.raises(AssistantError) as caught:
        await run(transport, Tokens())
    assert caught.value.code == code
    assert TOKEN not in str(caught.value)


async def test_gzip_encoded_stream_and_error_body_are_decoded():
    transport = Transport([done(open_item()), done(message(web_answer())), completed([])],
                          compress=True)
    response = await run(transport, Tokens(), schema=RESULT)
    assert response["evidence"]["opened"] == [{"url": URL, "reference": "ws_open1"}]
    assert checked_web(response)[0]["reference"] == "ws_open1"
    failing = Transport(status=429, compress=True,
                        body={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    with pytest.raises(AssistantError) as caught:
        await run(failing, Tokens())
    assert caught.value.code == "model_usage_limit"
    assert caught.value.diagnostics == {"http_status": 429, "server_code": "model_usage_limit"}


async def test_sse_framing_keeps_comments_ids_and_split_utf8_chunks():
    raw = b"".join([
        b": keep-alive\n\n",
        b"event: response.output_item.done\nid: 1\ndata: "
        + json.dumps(done(open_item())).encode() + b"\n\n",
        b"id: 2\ndata: " + json.dumps(done(message(web_answer())), ensure_ascii=False).encode() + b"\n\n",
        b"event: response.completed\ndata: " + json.dumps(completed([])).encode() + b"\n\n",
    ])
    cut = raw.index("文档".encode()) + 1
    transport = Transport(chunks=[raw[:cut], raw[cut:]])
    response = await run(transport, Tokens(), schema=RESULT)
    assert response["result"]["sources"][0]["title"] == "Python 文档"
    assert response["evidence"]["opened"] == [{"url": URL, "reference": "ws_open1"}]
    assert response["evidence"]["web_calls"] == 1


async def test_optional_fields_restore_through_the_strict_transport():
    answer = json.dumps({"value": 42, "note": None})
    transport = Transport([done(message(answer)), completed()])
    result = await run(transport, Tokens(), schema=OPTIONAL)
    assert result["result"] == {"value": 42}
    note = transport.requests[0]["body"]["text"]["format"]["schema"]["properties"]["note"]
    assert note["anyOf"][1] == {"type": "null"}


async def test_overlong_stream_is_rejected_by_real_byte_count(monkeypatch):
    monkeypatch.setattr(plan_web, "MAX_STREAM_BYTES", 64)
    transport = Transport([done(message(json.dumps({"value": 42})))])
    with pytest.raises(AssistantError) as caught:
        await run(transport, Tokens())
    assert caught.value.code == "model_output_size"


async def test_local_images_are_rejected_before_any_request():
    transport = Transport()
    with pytest.raises(AssistantError) as caught:
        await run(transport, Tokens(), images=[b"synthetic-png"])
    assert caught.value.code == "model_input_unsupported"
    assert not transport.requests


async def test_guard_revocation_is_detected_before_response_headers():
    transport = Transport(silent_headers=True)
    state = {"revoked": False}

    def guard():
        if state["revoked"]:
            raise AssistantError("model_auth", "授权变化")

    task = asyncio.create_task(run(transport, Tokens(), guard=guard))
    async with asyncio.timeout(3):
        while not transport.requests:
            await asyncio.sleep(0.01)
    state["revoked"] = True
    with pytest.raises(AssistantError) as caught:
        await asyncio.wait_for(task, 5)
    assert caught.value.code == "model_auth"
    assert transport.cancelled is True


async def test_guard_revocation_closes_a_silent_stream():
    transport = Transport([done(message(json.dumps({"value": 42})))], silent=True)
    state = {"revoked": False}

    def guard():
        if state["revoked"]:
            raise AssistantError("model_auth", "授权变化")

    task = asyncio.create_task(run(transport, Tokens(), guard=guard))
    async with asyncio.timeout(3):
        while not transport.streams:
            await asyncio.sleep(0.01)
    state["revoked"] = True
    with pytest.raises(AssistantError) as caught:
        await asyncio.wait_for(task, 5)
    assert caught.value.code == "model_auth"
    assert transport.streams[0].closed


async def test_cancel_closes_the_stream_before_returning():
    transport = Transport([done(message(json.dumps({"value": 42})))], silent=True)
    task = asyncio.create_task(run(transport, Tokens()))
    async with asyncio.timeout(3):
        while not transport.streams:
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.streams[0].closed


async def test_phase_cycle_is_restored_on_failure_and_success():
    phases = []
    transport = Transport([done(message(json.dumps({"value": 42}))), completed()])
    await run(transport, Tokens(), phase=phases.append)
    failing = Transport(status=401, body={"error": {"code": "unauthorized"}})
    with pytest.raises(AssistantError):
        await run(failing, Tokens(), phase=phases.append)
    assert phases == ["cycle", "cycle"]
