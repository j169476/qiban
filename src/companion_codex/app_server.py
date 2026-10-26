"""Bounded stdio client for a disposable, OS-isolated Codex app-server.

Transport messages and auth are memory-only. Nothing returned by the model may
select a local file, process command, login scope, or privileged protocol method.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path

from jsonschema import ValidationError
from jsonschema import validate as validate_json
from PIL import Image

from companion_core.errors import AssistantError

from .isolation import DISABLED_FEATURES, IsolatedJob, login_params, model_settings
from .process_scope import ProcessScope


class RpcProcess:
    @classmethod
    async def open(cls, *args, **kwargs):
        # Construction includes host IPC and a suspended child process. Keep it
        # off the web loop, but always collect/close the child on cancellation.
        task = asyncio.create_task(asyncio.to_thread(cls, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                client = await task
            except Exception:
                pass
            else:
                await asyncio.to_thread(client.close)
            raise

    def __init__(self, command, *, cwd, env, guard, deadline, max_bytes=8_000_000):
        self.guard, self.deadline, self.max_bytes = guard, deadline, max_bytes
        self.incoming, self.outgoing = queue.Queue(maxsize=128), queue.Queue(maxsize=8)
        self.failed = threading.Event()
        self.error = None
        self.stopping = threading.Event()
        self.scope, self.process = ProcessScope(), None
        self.threads = []
        self.counter = 0
        self._receive_guard_due = 0.0
        try:
            guard()
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=cwd, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | (0x4 if os.name == "nt" else 0), start_new_session=os.name != "nt",
            )
            self.scope.attach(self.process)
            guard()
            self.scope.resume()
            for target in (self._read, self._write):
                thread = threading.Thread(target=target, daemon=True)
                thread.start()
                self.threads.append(thread)
        except BaseException:
            self.close()
            raise

    def _fail(self, code):
        self.error = code
        self.failed.set()

    def _read(self):
        total = 0
        try:
            while not self.stopping.is_set():
                line = self.process.stdout.readline(self.max_bytes + 1)
                if not line:
                    self._fail("model_process_exit")
                    return
                total += len(line)
                if total > self.max_bytes:
                    self._fail("model_output_size")
                    return
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError()
                # Backpressure keeps the queue bounded without mistaking a
                # burst of small streaming events for an oversized response.
                while not self.stopping.is_set():
                    try:
                        self.incoming.put(row, timeout=.1)
                        break
                    except queue.Full:
                        if time.monotonic() >= self.deadline:
                            self._fail("model_timeout")
                            return
        except (OSError, ValueError):
            self._fail("model_protocol")

    def _write(self):
        try:
            while not self.stopping.is_set():
                try:
                    payload = self.outgoing.get(timeout=.1)
                except queue.Empty:
                    continue
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                del payload
        except (OSError, ValueError):
            self._fail("model_protocol")

    def check(self, *, check_guard=True):
        if check_guard:
            self.guard()
        if time.monotonic() >= self.deadline:
            raise AssistantError("model_timeout", "本轮模型处理超时")
        if self.failed.is_set():
            raise AssistantError(self.error, "隔离模型通信失败；未启用非隔离回退")

    def send(self, value):
        self.check()
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(payload) > 4_000_000:
            raise AssistantError("model_input_size", "本轮模型输入超出上限")
        try:
            self.outgoing.put_nowait(payload)
        except queue.Full:
            raise AssistantError("model_protocol", "隔离模型通信队列已满") from None

    async def receive(self):
        while True:
            # Host/database guards may perform synchronous IPC. Running one per
            # token starves the stream consumer. Poll them at most every 100 ms
            # while draining; deadline/failure checks still run for every event.
            # send() and final action authorization retain their full checks.
            check_guard = time.monotonic() >= self._receive_guard_due
            if check_guard:
                await asyncio.to_thread(self.check)
            else:
                self.check(check_guard=False)
            if check_guard:
                self._receive_guard_due = time.monotonic() + .1
            try:
                return self.incoming.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.02)

    async def call(self, method, params, on_event=lambda row: None):
        self.counter += 1
        request_id = self.counter
        await asyncio.to_thread(self.send, {"id": request_id, "method": method, "params": params})
        while True:
            row = await self.receive()
            if row.get("id") == request_id and "method" not in row:
                if "error" in row:
                    raise AssistantError("model_protocol", "Codex 拒绝隔离工作进程请求")
                return row.get("result", {})
            self.reject_request(row)
            on_event(row)

    def reject_request(self, row):
        if "id" not in row or "method" not in row:
            return
        # A refresh is delegated to the trusted host on a future attempt. The
        # worker cannot request a broader login, tool approval or filesystem grant.
        self.send({"id": row["id"], "error": {"code": -32000, "message": "Host authorization required"}})
        code = "model_auth" if row["method"] == "account/chatgptAuthTokens/refresh" else "model_tool_denied"
        raise AssistantError(code, "模型请求超出本轮授权；请检查 Codex 登录或隔离配置")

    def close(self):
        self.stopping.set()
        try:
            self.scope.close()
        finally:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=3)
                for thread in self.threads:
                    thread.join(timeout=2)
                for stream in (self.process.stdin, self.process.stdout):
                    stream.close()
            # Drop queued access tokens and model inputs immediately.
            while not self.outgoing.empty():
                try:
                    self.outgoing.get_nowait()
                except queue.Empty:
                    break


def copy_inputs(job, images):
    if len(images) > 12:
        raise AssistantError("model_input_size", "本轮图片超过上限")
    result = []
    for index, source in enumerate(images):
        path = Path(source)
        try:
            if path.is_symlink() or path.stat().st_size > 20_000_000:
                raise ValueError()
            # Decode and re-encode to remove metadata, embedded unrelated content
            # and original paths. The caller, never model text, chooses this file.
            with Image.open(path) as picture:
                if picture.width * picture.height > 25_000_000:
                    raise ValueError()
                target = job.work / f"input-{index}.png"
                picture.convert("RGBA" if "A" in picture.getbands() else "RGB").save(target)
            result.append({"type": "localImage", "path": str(target)})
        except (OSError, ValueError, Image.DecompressionBombError):
            raise AssistantError("model_image", "提供的图片无法安全加载") from None
    return result


class TurnEvidence:
    def __init__(self, *, native=None):
        self.native, self.thread_id, self.turn_id = native, None, None
        self.answer, self.completed, self.usage = "", False, {}
        self.trace = {"thread_id": "", "opened": [], "started": time.time(), "web_calls": 0}
        self.images = []
        self.items = set()

    def update(self, row):
        params = row.get("params", {})
        # WarningNotification permits threadId=null for a process warning. It
        # carries no task result/tool evidence. Scoped warnings still must match.
        if row.get("method") == "warning" and params.get("threadId") is None:
            return
        if self.thread_id and params.get("threadId", self.thread_id) != self.thread_id:
            raise AssistantError("model_protocol", "模型事件不属于本次会话")
        if self.turn_id and params.get("turnId", self.turn_id) != self.turn_id:
            raise AssistantError("model_protocol", "模型事件不属于本次轮次")
        method = row.get("method")
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {}).get("total", {})
            keys = {"input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
                    "output_tokens": "outputTokens", "reasoning_output_tokens": "reasoningOutputTokens"}
            self.usage = {key: usage.get(value) for key, value in keys.items()}
            self.usage["usage_events"] = 1
        if method == "turn/completed":
            turn = params.get("turn", {})
            if self.turn_id and turn.get("id") != self.turn_id:
                raise AssistantError("model_protocol", "模型完成事件轮次不一致")
            if turn.get("status") != "completed":
                raise AssistantError("model_failed", "隔离模型调用未完成")
            self.completed = True
        if method not in ("item/started", "item/completed"):
            return
        item = params.get("item", {})
        kind = item.get("type")
        permitted = {"userMessage", "agentMessage", "reasoning", "plan"}
        if self.native == "web":
            permitted.add("webSearch")
        if self.native == "image":
            permitted.add("imageGeneration")
        if kind not in permitted:
            raise AssistantError("model_tool_denied", "隔离模型尝试使用未授权能力")
        if method != "item/completed" or item.get("id") in self.items:
            return
        self.items.add(item.get("id"))
        if kind == "agentMessage" and item.get("phase") in (None, "final_answer"):
            self.answer = item.get("text", "")
        if kind == "webSearch":
            from .native import public_url

            self.trace["web_calls"] += 1
            if self.trace["web_calls"] > 6:
                raise AssistantError("native_tool_limit", "本轮网页工具调用次数超限")
            action = item.get("action") or {}
            if action.get("type") in ("openPage", "open_page"):
                for source in item.get("results") or []:
                    if not isinstance(source, dict) or not source.get("ref_id"):
                        continue
                    try:
                        url = public_url(source.get("url", ""))
                    except AssistantError:
                        continue
                    if url not in [entry["url"] for entry in self.trace["opened"]]:
                        self.trace["opened"].append({"url": url, "reference": source["ref_id"]})
        if kind == "imageGeneration" and item.get("savedPath") and item.get("status") == "completed":
            self.images.append(item["savedPath"])


def verify_configuration(config, native):
    features = config.get("features") or {}
    wanted = {key: key == "image_generation" and native == "image" for key in DISABLED_FEATURES}
    if (any(features.get(key) is not value for key, value in wanted.items())
            or config.get("cli_auth_credentials_store") != "ephemeral"
            or config.get("project_doc_max_bytes") != 0 or config.get("mcp_servers")
            or (config.get("tools") or {}).get("view_image") is True
            or config.get("web_search") != ("live" if native == "web" else "disabled")):
        raise AssistantError("model_isolation", "Codex 有效权限或认证存储与隔离配置不一致")


async def generate(instructions, context, output_schema, images=(), *, native=None,
                   store, policy, guard, phase, observation, reasoning_effort=None, model=None):
    from companion_core.structured_output import restore_optional, strict_schema

    from .isolated_artifacts import export_image

    await asyncio.to_thread(guard)
    work = store.runtime / "worker"
    deadline = time.monotonic() + policy["worker_timeout_seconds"]
    evidence = TurnEvidence(native=native)
    async with AsyncExitStack() as cleanup:
        cleanup.callback(phase, "cycle")
        cleanup.callback(lambda: observation.tokens(evidence.usage))
        settings = model_settings()
        if model is not None:
            settings["model"] = model
            # A selected model must not inherit an unrelated global effort.
            settings.pop("model_reasoning_effort", None)
        if reasoning_effort is not None:
            settings["model_reasoning_effort"] = reasoning_effort
        job = IsolatedJob(work, native=native, settings=settings)
        cleanup.push_async_callback(asyncio.to_thread, job.close)
        supplied_images = copy_inputs(job, images)
        client = await RpcProcess.open(job.command(), cwd=job.work, env=job.environment(), guard=guard,
                                       deadline=deadline, max_bytes=32_000_000 if native == "image" else 8_000_000)
        cleanup.push_async_callback(asyncio.to_thread, client.close)
        initialized = await client.call("initialize", {
            "clientInfo": {"name": "companion_isolated", "version": "0.6.0"},
            "capabilities": {"experimentalApi": True},
        })
        if Path(initialized.get("codexHome", "")).resolve() != job.home:
            raise AssistantError("model_isolation", "Codex 未使用隔离配置目录")
        await asyncio.to_thread(client.send, {"method": "initialized"})
        configuration = await client.call("config/read", {"includeLayers": False})
        verify_configuration(configuration.get("config", {}), native)
        await client.call("account/login/start", login_params())
        value = await client.call("thread/start", {
            "cwd": str(job.work), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
            "developerInstructions": instructions, "environments": [], "selectedCapabilityRoots": [],
        }, evidence.update)
        evidence.thread_id = value["thread"]["id"]
        evidence.trace["thread_id"] = evidence.thread_id
        servers = await client.call("mcpServerStatus/list", {})
        if servers.get("data") != [] or servers.get("nextCursor"):
            raise AssistantError("model_isolation", "隔离会话意外加载了外部工具服务")
        value = await client.call("turn/start", {
            "threadId": evidence.thread_id,
            "input": [{"type": "text", "text": json.dumps(context, ensure_ascii=False)}, *supplied_images],
            "outputSchema": strict_schema(output_schema),
            **({"effort": reasoning_effort} if reasoning_effort is not None else {}),
            **({"model": model} if model is not None else {}),
        }, evidence.update)
        evidence.turn_id = value["turn"]["id"]
        while not evidence.completed:
            row = await client.receive()
            client.reject_request(row)
            evidence.update(row)
        await asyncio.to_thread(guard)
        if len(evidence.answer.encode("utf-8")) > 160_000:
            raise AssistantError("model_output_size", "模型输出超出本轮上限")
        try:
            decision = restore_optional(json.loads(evidence.answer), output_schema)
            validate_json(decision, output_schema)
        except (ValueError, ValidationError):
            raise AssistantError("model_output_invalid", "模型结果未通过结构化协议校验") from None
        if native == "image" and decision.get("image_path"):
            export_image(job, evidence, decision, store.runtime)
        if native:
            return {"result": decision, "evidence": evidence.trace}
        return decision
