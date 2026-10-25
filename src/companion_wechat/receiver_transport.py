"""Bounded private pipes to a hidden native database worker; no network listener.

The child alone holds WeChat database keys. Its stdout is a private transport
carrying allowed messages, never a terminal log. Raw exceptions are discarded.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from companion_core.errors import AssistantError

RETRY_CODES = frozenset({
    "source_catalog_changed", "source_pending_at_enrollment", "database_busy", "query_timeout",
    "sidecars_unavailable", "client_not_running", "client_ambiguous", "process_access_denied",
    "process_closed", "process_query_failed", "scan_timeout", "key_not_found", "key_scan_failed",
    "source_unavailable", "database_unreadable", "connection_closed", "worker_disconnected",
    "worker_timeout", "worker_start_failed",
})
ERROR_CODES = RETRY_CODES | frozenset({
    "source_configuration_invalid", "source_closed", "source_generation_changed", "source_identity_changed",
    "self_identity_unverified", "source_schema_unsupported", "source_checkpoint_invalid", "source_anchor_changed",
    "source_read_failed", "source_enrollment_invalid", "worker_protocol_invalid", "worker_not_open",
    "worker_already_open", "configuration_invalid", "database_path_invalid", "database_format_unsupported",
    "contact_schema_unsupported", "target_missing_or_ambiguous", "target_kind_mismatch",
    "message_schema_unsupported", "sender_schema_unsupported", "database_count_unsupported",
    "unsupported_platform", "invalid_configuration", "client_version_unsupported", "client_identity_changed",
    "read_budget_exceeded", "scan_limit_exceeded", "invalid_memory_address", "invalid_database_page",
    "window_query_failed", "dependency_missing", "dependency_version_unsupported", "path_invalid",
    "key_invalid", "dependency_unavailable", "query_denied", "query_failed", "worker_shutdown_failed",
    "receipt_invalid_input", "receipt_invalid_report", "receipt_reader_failed", "receipt_reader_changed",
    "receipt_scope_changed", "receipt_catalog_changed", "receipt_generation_changed", "receipt_anchor_changed",
    "receipt_watermark_expired", "receipt_server_id_reused", "receipt_duplicate_record",
    "receipt_action_busy", "receipt_capacity_exceeded", "receipt_closed", "receipt_source_pending",
})


class WorkerError(AssistantError):
    def __init__(self, code):
        code = code if code in ERROR_CODES else "worker_protocol_invalid"
        self.recoverable = code in RETRY_CODES
        super().__init__(code, "微信只读数据进程暂不可用；请按状态码检查，原有游标已保留")


class SubprocessTransport:
    MAX_REQUEST = 262144
    MAX_REPLY = 2 * 1024 * 1024

    def __init__(self, *, python=None, open_timeout=150.0, read_timeout=20.0):
        self.python = str(python or sys.executable)
        self.open_timeout, self.read_timeout = open_timeout, read_timeout
        self.process = None
        self.sequence = 0
        self._lock = asyncio.Lock()
        self._process_lock = threading.RLock()
        self._closed = False

    def _spawn(self):
        with self._process_lock:
            self._spawn_locked()

    def _spawn_locked(self):
        if self._closed:
            raise WorkerError("worker_disconnected")
        if self.process is not None:
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        try:
            self.process = subprocess.Popen(
                [self.python, "-u", "-m", "companion_wechat.db_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, bufsize=0, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            raise WorkerError("worker_start_failed") from None

    def _stop(self):
        with self._process_lock:
            self._stop_locked()

    def _stop_locked(self):
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            raise WorkerError("worker_shutdown_failed")
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    def _exchange(self, request, timeout):
        try:
            encoded = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            if len(encoded) > self.MAX_REQUEST:
                raise WorkerError("worker_protocol_invalid")
        except (ValueError, TypeError, UnicodeError):
            raise WorkerError("worker_protocol_invalid") from None
        self._spawn()
        results = queue.Queue(maxsize=1)

        def exchange():
            try:
                remaining = memoryview(encoded)
                while remaining:
                    written = self.process.stdin.write(remaining)
                    if not written:
                        raise OSError("pipe closed")
                    remaining = remaining[written:]
                self.process.stdin.flush()
                results.put(self.process.stdout.readline(self.MAX_REPLY + 1))
            except Exception:
                results.put(None)

        thread = threading.Thread(target=exchange, name="wechat-private-pipe", daemon=True)
        thread.start()
        try:
            try:
                raw = results.get(timeout=timeout)
            except queue.Empty:
                raise WorkerError("worker_timeout") from None
            if not raw:
                raise WorkerError("worker_disconnected")
            if len(raw) > self.MAX_REPLY or not raw.endswith(b"\n"):
                raise WorkerError("worker_protocol_invalid")
            try:
                response = json.loads(raw)
            except (UnicodeError, ValueError):
                raise WorkerError("worker_protocol_invalid") from None
            if (not isinstance(response, dict) or type(response.get("id")) is not int
                    or response["id"] != request["id"] or type(response.get("ok")) is not bool):
                raise WorkerError("worker_protocol_invalid")
            if response["ok"]:
                if set(response) != {"id", "ok", "result"} or not isinstance(response["result"], dict):
                    raise WorkerError("worker_protocol_invalid")
                return response["result"]
            code = response.get("error")
            if not isinstance(code, str):
                code = "worker_protocol_invalid"
            raise WorkerError(code)
        except BaseException:
            self._stop()
            raise
        finally:
            thread.join(timeout=0.25)

    async def request(self, command, **arguments):
        async with self._lock:
            self.sequence += 1
            request = {"id": self.sequence, "command": command, **arguments}
            timeout = self.open_timeout if command == "open" else self.read_timeout
            try:
                return await asyncio.to_thread(self._exchange, request, timeout)
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.to_thread(self._stop))
                raise

    async def close(self):
        # Killing a worker closes all its handles even if a native query hangs.
        await asyncio.to_thread(self._stop)
