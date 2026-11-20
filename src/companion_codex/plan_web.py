"""Direct SIWC Responses adapter for the desktop web-research step only.

Every other model step keeps using the isolated Codex app-server. This adapter
reuses the application's existing account snapshot, serial admission, task guard
and observation ledger; it never reads a global Codex login or an API key, never
retries, never switches account/model and never falls back to another route.
Closing the local stream only stops our local read: it is not a claim that
remote inference or billing stopped immediately.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack

import httpx
from jsonschema import ValidationError
from jsonschema import validate as validate_json

from companion_core.errors import AssistantError
from companion_core.structured_output import restore_optional, strict_schema

from .failures import MESSAGES
from .native import public_url

ENDPOINT = "https://api.openai.com/v1/responses"
GUARD_INTERVAL = 0.5
MAX_WEB_CALLS = 6
MAX_INPUT_BYTES = 4_000_000
MAX_STREAM_BYTES = 8_000_000
MAX_ERROR_BYTES = 32_000
MAX_ANSWER_BYTES = 160_000
# Item kinds the approved step may ever hold; anything else stops the request.
PERMITTED_ITEMS = frozenset({"web_search_call", "message", "reasoning"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
# Exact structured codes only; server prose, request text and credentials stay out.
FAILURE_CODES = {
    "usage_limit_reached": "model_usage_limit",
    "usage_limit_exceeded": "model_usage_limit",
    "insufficient_quota": "model_usage_limit",
    "subscription_sharing_usage_limit_exceeded": "model_usage_limit",
    "subscription_sharing_usage_unavailable": "model_usage_limit",
    "rate_limit_exceeded": "model_rate_limit",
    "rate_limit_error": "model_rate_limit",
    "invalid_request_error": "model_request_invalid",
    "context_length_exceeded": "model_input_size",
    "model_not_found": "model_unavailable",
    "server_error": "model_temporary",
}
HTTP_CODES = {400: "model_request_invalid", 401: "model_auth", 403: "model_permission",
              404: "model_unavailable", 429: "model_rate_limit"}
HTTP_STRUCTURED = frozenset({"model_usage_limit", "model_rate_limit", "model_input_size",
                             "model_unavailable"})
LOCAL = {
    "model_protocol": "网页模型流不符合预期协议；未切换其他执行路线。",
    "model_tool_denied": "模型使用了未授权的工具，本轮已停止。",
    "model_input_unsupported": "此网页模型步骤不支持图片等本地输入。",
    "native_tool_limit": "本轮网页工具调用次数超限",
    "model_output_invalid": "模型结果未通过结构化协议校验",
    "model_output_size": "模型输出超出本轮上限",
}


def _failure(code, diagnostics=None):
    message = MESSAGES.get(code) or LOCAL[code]
    failure = AssistantError(code, message)
    if diagnostics:
        failure.diagnostics = diagnostics
    return failure


def _structured(payload):
    """Map a structured error code/type to a public code; unknown values stay unknown."""
    info = payload if isinstance(payload, dict) else {}
    if isinstance(info.get("error"), dict):
        info = info["error"]
    for key in ("code", "type"):
        value = info.get(key)
        if isinstance(value, str) and value in FAILURE_CODES:
            return FAILURE_CODES[value]
    return None


def stream_failure(kind, payload):
    code = _structured(payload) or ("model_interrupted" if kind == "response.incomplete"
                                    else "model_failed")
    return _failure(code, {"stream_event": kind})


def http_failure(status, payload):
    if type(status) is not int or not 100 <= status <= 599:
        return _failure("model_protocol")
    code = HTTP_CODES.get(status) or ("model_temporary" if status >= 500 else "model_failed")
    structured = _structured(payload)
    if status != 401 and structured in HTTP_STRUCTURED:
        code = structured
    diagnostics = {"http_status": status}
    if structured:
        diagnostics["server_code"] = structured
    return _failure(code, diagnostics)


def request_body(instructions, context, output_schema, model, reasoning_effort, service_tier=None):
    """Fixed request: one native web tool, JSON-schema text, streamed and unretained."""
    body = {
        "model": model,
        **({"service_tier": service_tier} if service_tier else {}),
        **({"reasoning": {"effort": reasoning_effort}} if reasoning_effort is not None else {}),
        "store": False,
        "stream": True,
        "instructions": instructions,
        "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "result", "strict": True,
                            "schema": strict_schema(output_schema)}},
        "tools": [{"type": "web_search", "external_web_access": True}],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
    }
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_INPUT_BYTES:
        raise _failure("model_input_size")
    return body


def _duration(policy):
    value = policy.get("worker_timeout_seconds") if isinstance(policy, dict) else None
    if type(value) not in (int, float) or not 0 < value <= 3600:
        raise AssistantError("model_config", "订阅网页模型参数无效。")
    return float(value)


def headers(token):
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "text/event-stream"}


def _merge(previous, current):
    """A terminal item may only add detail; it never erases observed evidence."""
    merged = dict(previous)
    for key, value in current.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key == "status" and merged.get("status") == "completed" and value != "completed":
            continue
        merged[key] = value
    return merged


class Evidence:
    """Streamed items are authoritative: a terminal response may carry none at all."""

    def __init__(self, model):
        self.model, self.started = model, time.time()
        self.response_id, self.answer, self.usage = "", "", {}
        self.items, self.order, self.calls, self.anonymous = {}, [], [], 0
        self.completed, self.bytes = False, 0

    @staticmethod
    def identifier(value):
        return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else ""

    def count(self):
        return len(self.calls) + self.anonymous

    def text(self, value):
        if len(value.encode("utf-8")) > MAX_ANSWER_BYTES:
            raise _failure("model_output_size")
        self.answer = value

    def add(self, item, *, done):
        kind = item.get("type")
        if kind not in PERMITTED_ITEMS:
            raise _failure("model_tool_denied")
        key = self.identifier(item.get("id"))
        if kind == "web_search_call":
            if key and key not in self.calls:
                self.calls.append(key)
            elif not key:
                self.anonymous += 1
            if self.count() > MAX_WEB_CALLS:
                raise _failure("native_tool_limit")
        if not key:
            return
        if key in self.items:
            item = _merge(self.items[key], item)
        else:
            self.order.append(key)
        self.items[key] = item
        if kind == "message" and done:
            parts = [part["text"] for part in item.get("content") or []
                     if isinstance(part, dict) and part.get("type") == "output_text"
                     and isinstance(part.get("text"), str)]
            if parts:
                self.text("".join(parts))

    def opened(self):
        """Only a completed open_page action of this response proves a page was opened."""
        result = []
        for key in self.order:
            item = self.items[key]
            if item.get("type") != "web_search_call" or item.get("status") != "completed":
                continue
            action = item.get("action")
            if not isinstance(action, dict) or action.get("type") != "open_page":
                continue
            try:
                url = public_url(action.get("url"))
            except AssistantError:
                continue
            if url not in [entry["url"] for entry in result]:
                result.append({"url": url, "reference": key})
        return result

    def usage_of(self, value):
        if not isinstance(value, dict):
            return
        inside = value.get("input_tokens_details")
        outside = value.get("output_tokens_details")
        self.usage = {
            "input_tokens": value.get("input_tokens"),
            "cached_input_tokens": inside.get("cached_tokens") if isinstance(inside, dict) else None,
            "output_tokens": value.get("output_tokens"),
            "reasoning_output_tokens": outside.get("reasoning_tokens") if isinstance(outside, dict) else None,
            "usage_events": 1,
        }

    def absorb(self, event):
        kind = event.get("type")
        if kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item")
            if not isinstance(item, dict):
                raise _failure("model_protocol")
            self.add(item, done=kind == "response.output_item.done")
        elif kind == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                self.text(self.answer + delta)
        elif kind == "response.output_text.done":
            value = event.get("text")
            if isinstance(value, str) and value.strip():
                self.text(value)
        elif kind in ("response.completed", "response.failed", "response.incomplete"):
            self.terminal(kind, event.get("response"))
        elif kind == "error":
            raise stream_failure("error", event)
        # response.web_search_call.* alone is never page-open proof and adds no count.

    def terminal(self, kind, response):
        if not isinstance(response, dict):
            raise _failure("model_protocol")
        self.usage_of(response.get("usage"))
        # Keep streamed items; merge the terminal list without counting a tool twice.
        for item in response.get("output") or []:
            if isinstance(item, dict):
                self.add(item, done=True)
        self.response_id = self.identifier(response.get("id"))
        if kind != "response.completed":
            raise stream_failure(kind, response)
        if response.get("model") != self.model:
            raise _failure("model_unavailable", {"stream_event": kind, "model_mismatch": True})
        self.completed = True


def _event(data):
    if data in ("[DONE]", ""):
        return None
    try:
        event = json.loads(data)
    except ValueError:
        raise _failure("model_protocol") from None
    if not isinstance(event, dict):
        raise _failure("model_protocol")
    return event


def _line(text, state):
    """Consume one SSE line; returns True when the stream is finished.

    Only a blank line dispatches the accumulated data, so event/id/comment
    fields may sit between data lines without flushing them early.
    """
    if text.startswith("data:"):
        state["data"].append(text[5:].lstrip())
        return False
    if text or not state["data"]:
        return False
    event, state["data"] = _event("\n".join(state["data"])), []
    if event is None:
        return True
    state["evidence"].absorb(event)
    return state["evidence"].completed


async def _read(response, evidence, deadline):
    """Count decompressed bytes, not characters, so the stream stays bounded."""
    state, buffer = {"data": [], "evidence": evidence}, b""
    async for chunk in response.aiter_bytes():
        evidence.bytes += len(chunk)
        if evidence.bytes > MAX_STREAM_BYTES:
            raise _failure("model_output_size")
        if time.monotonic() >= deadline:
            raise AssistantError("model_timeout", "本轮模型处理超时")
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            text = raw.decode("utf-8", "replace")
            if text.endswith("\r"):
                text = text[:-1]
            if _line(text, state):
                return


async def _error_payload(response):
    """Read a bounded decompressed error body; raw text is never retained."""
    total, chunks = 0, []
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_ERROR_BYTES:
                return {}
            chunks.append(chunk)
    except (httpx.HTTPError, OSError, ValueError):
        return {}
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        return {}


async def _watch(guard, deadline):
    """Keep revocation responsive even when the server sends nothing at all."""
    while True:
        if time.monotonic() >= deadline:
            raise AssistantError("model_timeout", "本轮模型处理超时")
        await asyncio.to_thread(guard)
        remaining = deadline - time.monotonic()
        await asyncio.sleep(GUARD_INTERVAL if remaining > GUARD_INTERVAL else max(0.05, remaining))


async def _request(body, token, evidence, deadline, transport):
    """Whole HTTP exchange: connect, headers, error body and stream share one budget."""
    chosen = httpx.AsyncHTTPTransport(retries=0) if transport is None else transport
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(max(0.1, deadline - time.monotonic())),
                                    trust_env=False, follow_redirects=False,
                                    transport=chosen) as client:
            async with client.stream("POST", ENDPOINT, headers=headers(token), json=body) as response:
                if response.status_code != 200:
                    raise http_failure(response.status_code, await _error_payload(response))
                await _read(response, evidence, deadline)
    except (httpx.TimeoutException, TimeoutError):
        raise AssistantError("model_timeout", "本轮模型处理超时") from None
    except httpx.HTTPError:
        # Fixed public classification; the transport exception stays out of logs.
        raise _failure("model_temporary") from None


async def _consume(evidence, guard, deadline, attempt):
    """Run the request beside a guard watcher; the loser is closed before return."""
    reader = asyncio.create_task(attempt())
    watcher = asyncio.create_task(_watch(guard, deadline))
    try:
        done, _ = await asyncio.wait({reader, watcher}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in (reader, watcher):
            if not task.done():
                task.cancel()
        # Local stream/client cleanup must finish before admission is released.
        await asyncio.gather(reader, watcher, return_exceptions=True)
    if not evidence.completed:
        raise _failure("model_interrupted")


def _decision(evidence, output_schema):
    if not evidence.answer:
        raise _failure("model_output_invalid")
    try:
        decision = restore_optional(json.loads(evidence.answer), output_schema)
        validate_json(decision, output_schema)
    except (ValueError, ValidationError):
        raise _failure("model_output_invalid") from None
    return decision


async def generate(instructions, context, output_schema, images=(), *, policy, guard, observation,
                   access, model=None, reasoning_effort=None, phase=lambda value: None,
                   transport=None, service_tier=None):
    if images:
        raise _failure("model_input_unsupported")
    if (not isinstance(model, str) or not IDENTIFIER.fullmatch(model)
            or reasoning_effort is not None and (
                not isinstance(reasoning_effort, str) or not IDENTIFIER.fullmatch(reasoning_effort))):
        raise AssistantError("model_config", "订阅网页模型参数无效。")
    token = getattr(access, "access_token", "")
    if not isinstance(token, str) or not token:
        raise _failure("model_auth")
    body = request_body(instructions, context, output_schema, model, reasoning_effort, service_tier)
    seconds = _duration(policy)
    deadline = time.monotonic() + seconds
    evidence = Evidence(model)
    async with AsyncExitStack() as cleanup:
        cleanup.callback(phase, "cycle")
        # Usage received before a failure is still recorded; subsets are not re-added.
        cleanup.callback(lambda: observation.tokens(evidence.usage))
        try:
            async with asyncio.timeout(seconds):
                await asyncio.to_thread(guard)
                await _consume(evidence, guard, deadline,
                               lambda: _request(body, token, evidence, deadline, transport))
                await asyncio.to_thread(guard)
                decision = _decision(evidence, output_schema)
        except TimeoutError:
            raise AssistantError("model_timeout", "本轮模型处理超时") from None
        return {"result": decision,
                "evidence": {"thread_id": evidence.response_id, "opened": evidence.opened(),
                             "started": evidence.started, "web_calls": evidence.count()}}
