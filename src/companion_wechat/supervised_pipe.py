"""Bounded private stdio protocol for a human-supervised native submission.

Only the controlling parent may interpret these messages. This module never
operates a window, sends a key, opens a network listener, or writes a body file.
Owned reader/writer threads are stopped before native pipe shutdown.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict

from companion_core.errors import AssistantError

from .contracts import digest, identifier, require
from .live_sender import DriverOutcome, LiveSenderGateway

MAX_LINE = 16384
MAX_TOTAL = 262144
MAX_MESSAGES = 128
PIPE_ERRORS = frozenset({"wechat_pipe_required", "wechat_pipe_closed", "wechat_pipe_protocol",
                         "wechat_pipe_limit", "wechat_pipe_timeout", "wechat_pipe_failed"})


def require_private_stdio(reader, writer):
    """Reject terminals and redirected files before any private message exists."""
    try:
        descriptors = (reader.fileno(), writer.fileno())
        if os.name == "nt":
            import ctypes
            import msvcrt

            get_type = ctypes.WinDLL("kernel32", use_last_error=True).GetFileType
            get_type.argtypes = [ctypes.c_void_p]
            get_type.restype = ctypes.c_uint32
            valid = all(get_type(msvcrt.get_osfhandle(fd)) == 3 for fd in descriptors)
        else:
            valid = all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in descriptors)
        require(valid, "wechat_pipe_required")
    except AssistantError:
        raise
    except Exception:
        raise AssistantError("wechat_pipe_required", "监督入口仅接受父进程私有管道") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError()


class PrivateJsonPipe:
    """Exactly one outstanding request, strict phases, finite memory and bytes."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.loop = asyncio.get_running_loop()
        self.incoming, self.outgoing = queue.Queue(maxsize=1), queue.Queue(maxsize=8)
        self._arrived, self._failed = asyncio.Event(), asyncio.Event()
        self._mutex, self._exchange_lock = threading.Lock(), asyncio.Lock()
        self._expected = None
        self._error = None
        self._stopping = threading.Event()
        self._started = False
        self._threads = []
        self._sent_bytes = self._sent_count = 0

    def __repr__(self):
        return "PrivateJsonPipe(<redacted>)"

    def _signal(self, event):
        try:
            self.loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    def _abort(self, code):
        with self._mutex:
            if self._error is None:
                self._error = code if code in PIPE_ERRORS else "wechat_pipe_failed"
        self._signal(self._failed)
        self._signal(self._arrived)

    def check(self):
        require(not self._stopping.is_set(), "wechat_pipe_closed")
        if self._error is not None:
            raise AssistantError(self._error, "监督管道已停止，禁止继续提交")

    def validate(self, condition):
        if not condition:
            self._abort("wechat_pipe_protocol")
        require(condition, "wechat_pipe_protocol")

    def start(self):
        self.check()
        if self._started:
            return
        self._started = True
        for target in (self._read, self._write):
            thread = threading.Thread(target=target, daemon=True)
            self._threads.append(thread)
            thread.start()

    def _read(self):
        total = count = 0
        try:
            while not self._stopping.is_set():
                line = self.reader.readline(MAX_LINE + 1)
                if not line:
                    self._abort("wechat_pipe_closed")
                    return
                total += len(line)
                count += 1
                if len(line) > MAX_LINE or total > MAX_TOTAL or count > MAX_MESSAGES:
                    self._abort("wechat_pipe_limit")
                    return
                if type(line) is not bytes or not line.endswith(b"\n"):
                    raise ValueError()
                message = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs,
                                     parse_constant=_invalid_constant)
                if (type(message) is not dict or type(message.get("v")) is not int or message["v"] != 1
                        or type(message.get("id")) is not str
                        or not re.fullmatch(r"[a-f0-9]{32}", message["id"])):
                    raise ValueError()
                fields = {"v", "type", "id"}
                if message.get("type") == "staged":
                    fields |= {"client_epoch", "view_token", "observed", "draft_matches", "manual_takeover"}
                elif message.get("type") == "claim":
                    fields.add("view_token")
                elif message.get("type") not in {"ready", "submitted"}:
                    raise ValueError()
                if set(message) != fields:
                    raise ValueError()
                with self._mutex:
                    if self._expected != (message["id"], message.get("type")):
                        raise ValueError()
                    self._expected = None
                self.incoming.put_nowait(message)
                self._signal(self._arrived)
        except (ValueError, UnicodeError, TypeError, RecursionError, queue.Full):
            self._abort("wechat_pipe_protocol")
        except Exception:
            self._abort("wechat_pipe_failed")

    def _write(self):
        while not self._stopping.is_set():
            value = self.outgoing.get()
            try:
                if value is None:
                    return
                self.writer.write(value)
                self.writer.flush()
            except Exception:
                self._abort("wechat_pipe_closed")
                return
            finally:
                self.outgoing.task_done()

    def publish(self, kind, payload, *, message_id=None):
        self.check()
        require(self._started, "wechat_pipe_failed")
        frame = {"v": 1, "type": kind, **payload}
        if message_id is not None:
            frame["id"] = message_id
        try:
            encoded = (json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
            require(len(encoded) <= MAX_LINE and self._sent_bytes + len(encoded) <= MAX_TOTAL
                    and self._sent_count < MAX_MESSAGES, "wechat_pipe_limit")
            self.outgoing.put_nowait(encoded)
            self._sent_bytes += len(encoded)
            self._sent_count += 1
        except AssistantError:
            self._abort("wechat_pipe_limit")
            raise
        except Exception:
            self._abort("wechat_pipe_failed")
            raise AssistantError("wechat_pipe_failed", "监督管道写入失败") from None

    async def exchange(self, kind, payload, *, expected, timeout, message_id=None):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 600,
                "wechat_pipe_protocol")
        async with self._exchange_lock:
            self.check()
            key = message_id or secrets.token_hex(16)
            with self._mutex:
                require(self._expected is None and self.incoming.empty(), "wechat_pipe_protocol")
                self._expected = (key, expected)
            self.start()
            self.publish(kind, payload, message_id=key)
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        self.check()
                        try:
                            return self.incoming.get_nowait()
                        except queue.Empty:
                            self._arrived.clear()
                            if not self.incoming.empty():
                                continue
                            await self._arrived.wait()
            except TimeoutError:
                self._abort("wechat_pipe_timeout")
                raise AssistantError("wechat_pipe_timeout", "监督输入超时，禁止继续提交") from None
            except asyncio.CancelledError:
                self._abort("wechat_pipe_closed")
                raise

    async def handshake(self, *, timeout=30.0):
        response = await self.exchange("ready", {"protocol": "wechat-supervised-v1"},
                                       expected="ready", timeout=timeout)
        require(set(response) == {"v", "type", "id"}, "wechat_pipe_protocol")

    async def supervise(self, coroutine):
        """EOF or invalid input cancels even a model currently awaiting output."""
        try:
            self.check()
        except Exception:
            coroutine.close()
            raise
        task = asyncio.create_task(coroutine)
        failure = asyncio.create_task(self._failed.wait())
        try:
            done, _pending = await asyncio.wait((task, failure), return_when=asyncio.FIRST_COMPLETED)
            if failure in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.check()
            return await task
        finally:
            for pending in (task, failure):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, failure, return_exceptions=True)

    async def flush(self):
        async with asyncio.timeout(1.0):
            while self.outgoing.unfinished_tasks:
                self.check()
                await asyncio.sleep(0.01)

    async def close(self):
        if self._stopping.is_set():
            return
        try:
            if self._error is None:
                await self.flush()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self._stopping.set()
            try:
                self.outgoing.put_nowait(None)
            except queue.Full:
                pass
            while not self.incoming.empty():
                self.incoming.get_nowait()
            await asyncio.to_thread(self._join_threads)

    def _join_threads(self):
        # A blocked CRT readline at interpreter shutdown can crash Windows.
        # Cancel only I/O owned by our threads; never close the parent's handles.
        api = None
        if os.name == "nt":
            import ctypes

            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            api.OpenThread.restype = ctypes.c_void_p
            api.CancelSynchronousIo.argtypes = [ctypes.c_void_p]
            api.CancelSynchronousIo.restype = ctypes.c_int
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            api.CloseHandle.restype = ctypes.c_int
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            alive = [thread for thread in self._threads if thread.is_alive()]
            if not alive:
                return
            for thread in alive:
                if api is not None and thread.native_id is not None:
                    handle = api.OpenThread(0x0001, False, thread.native_id)
                    if handle:
                        try:
                            api.CancelSynchronousIo(handle)
                        finally:
                            api.CloseHandle(handle)
                thread.join(timeout=0.025)


class SupervisedPipeOperator:
    """Parent observes/stages/submits; only the source DB confirms a send."""

    def __init__(self, pipe, receipts, *, prepare_timeout=180.0, submit_timeout=20.0,
                 confirm_seconds=5.0, sleep=asyncio.sleep):
        require(0 < prepare_timeout <= 600 and 0 < submit_timeout <= 30 and 0 < confirm_seconds <= 30,
                "wechat_pipe_protocol")
        self.pipe, self.receipts = pipe, receipts
        self.prepare_timeout, self.submit_timeout = prepare_timeout, submit_timeout
        self.confirm_seconds, self.sleep = confirm_seconds, sleep
        self._pending = None
        self._closed = False

    def __repr__(self):
        return "SupervisedPipeOperator(<redacted>)"

    async def prepare(self, request):
        from .supervised_sender import StagedProof

        require(not self._closed and self._pending is None, "wechat_pipe_protocol")
        LiveSenderGateway._request_valid(request)
        require(request.conversation in {"owner_main", "family"}, "wechat_pipe_protocol")
        response = await self.pipe.exchange("prepare", {"request": asdict(request), "text_digest": digest(request.text)},
                                            expected="staged", timeout=self.prepare_timeout)
        self.pipe.validate(set(response) == {"v", "type", "id", "client_epoch", "view_token", "observed",
                                  "draft_matches", "manual_takeover"}
                and identifier(response["client_epoch"]) and identifier(response["view_token"])
                and response["observed"] is True and response["draft_matches"] is True
                and response["manual_takeover"] is False)
        proof = StagedProof(request.account_identity, request.target_ref, response["client_epoch"],
                            response["view_token"], digest(request.text), observed=True,
                            draft_matches=True, manual_takeover=False)
        token = await self.receipts.begin(request)
        self._pending = (response["id"], request, proof, token)
        return proof

    async def dispatch_once(self, request, proof, *, authorize):
        require(not self._closed and self._pending is not None, "wechat_pipe_protocol")
        key, saved, expected, token = self._pending
        require(saved == request and expected == proof, "wechat_pipe_protocol")
        # Remove local dispatch authority before waiting; no repeat call can retry.
        self._pending = None
        try:
            claim = await self.pipe.exchange("armed", {"view_token": proof.view_token}, expected="claim",
                                             timeout=self.submit_timeout, message_id=key)
            self.pipe.validate(set(claim) == {"v", "type", "id", "view_token"}
                               and claim["view_token"] == proof.view_token)
            self.pipe.check()
            grant = authorize()
            require(type(grant) is dict and set(grant) == {"expires_unix_ms"}
                    and type(grant["expires_unix_ms"]) is int and grant["expires_unix_ms"] > 0,
                    "wechat_pipe_protocol")
            submitted = await self.pipe.exchange("grant", grant, expected="submitted",
                                                 timeout=self.submit_timeout, message_id=key)
            self.pipe.validate(set(submitted) == {"v", "type", "id"})
            try:
                async with asyncio.timeout(self.confirm_seconds):
                    while True:
                        self.pipe.check()
                        result = await self.receipts.confirm(request, token)
                        if (isinstance(result, DriverOutcome) and result.status == "confirmed"
                                and result.client_recorded is True):
                            return result
                        await self.sleep(0.25)
            except TimeoutError:
                return DriverOutcome("uncertain")
        finally:
            await self.receipts.discard(request, token)

    async def discard(self, proof):
        if self._pending is not None and self._pending[2] == proof:
            _key, request, _proof, token = self._pending
            self._pending = None
            await self.receipts.discard(request, token)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._pending is not None:
            await self.discard(self._pending[2])
