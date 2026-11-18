"""Owned hidden native QQ process with a private framed stdio channel.

Native SDK diagnostics are discarded, not parsed as messages or persisted.
Only the random per-process protocol prefix is accepted as a reply. Closing
the Windows Job ends owned descendants, never the user's desktop QQ process.
"""
import json
import os
import queue
import secrets
import subprocess
import threading
from pathlib import Path

from companion_core.errors import AssistantError
from companion_core.process_scope import ProcessScope
from companion_core.reply_timing import span as timing_span


READ_FAILURE_MESSAGES = {
    'qq_read_' + operation + '_' + reason: f'QQ {step}失败：{message}。'
    for operation, step in {
        'private_head': '读取好友最新消息', 'group_head': '读取群最新消息',
        'private_open': '读取好友历史消息', 'group_open': '读取群历史消息',
    }.items()
    for reason, message in {
        'group_head_timeout': '等待群消息同步超时', 'group_snapshot_unavailable': '群消息列表尚未就绪',
        'group_contact_unavailable': '群成员列表尚未就绪', 'group_contact_initialization_failed': '群成员列表加载失败',
        'group_checkpoint_incomplete': '上次读取位置不完整', 'checkpoint_lookup_failed': '找不到上次读取位置',
        'checkpoint_not_unique': '无法确定上次读取位置', 'checkpoint_identity_changed': '上次读取位置的消息已变化',
        'sync_generation_changed': '读取期间重新开始了同步', 'sync_timeout': '消息同步超时',
        'unclassified': 'QQ 未提供具体原因',
    }.items()
}


class NativeProcess:
    WORKER = "worker.cjs"
    MAX_REPLY = 16 * 1024 * 1024
    ERRORS = frozenset({"qq_login_required", "qq_login_timeout", "qq_target_unverified", "qq_native_runtime_version",
        "qq_native_session_failed", "qq_native_catalog_invalid", "qq_native_not_ready",
        "qq_native_protocol_invalid", "qq_native_read_failed", "qq_native_send_state", "qq_native_send_uncertain",
        "qq_media_invalid", "qq_media_state", "qq_media_limit", "qq_media_path", "qq_media_timeout",
        "qq_media_download_failed", "qq_media_integrity"}) | READ_FAILURE_MESSAGES.keys()

    def __init__(self, config, *, timeout=90):
        self.config, self.timeout = dict(config), timeout
        self.nonce = secrets.token_hex(32)
        self.prefix = ("QIBAN_NATIVE:" + self.nonce + ":").encode("ascii")
        self.process = self.scope = None
        self.sequence = 0
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self.closed = False
        self.last_failure = None
        self.media_stage = None
        self.media_root = None

    def open(self):
        with self._lifecycle_lock:
            if self.process is not None or self.closed:
                raise AssistantError("qq_native_process_state", "QQ 后台进程状态不允许启动")
            runtime = Path(self.config["runtime"]).resolve(strict=True)
            from .native_resources import execution_runtime

            runtime, working = execution_runtime(runtime, self.config["data"])
            self.config.update(runtime=str(runtime), data=str(working.parent))
            if self.WORKER == 'worker.cjs':
                from .native_staging import NativeStaging

                self.media_stage = NativeStaging(self.config['data'], self.nonce)
                self.media_stage.recover()
                self.media_root = self.media_stage.path
                self.config['media_session'] = self.nonce
            executable = runtime / "node.exe"
            worker = Path(__file__).with_name("native") / self.WORKER
            try:
                self.scope = ProcessScope()
                self.process = subprocess.Popen([str(executable), str(worker)], cwd=working,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | (4 if os.name == "nt" else 0),
                    start_new_session=os.name != "nt")
                self.scope.attach(self.process)
                if self.media_stage is not None:
                    self.media_stage.create(self.process.pid)
                self.scope.resume()
            except BaseException:
                self.close()
                raise
        try:
            return self.request("open", nonce=self.nonce, config=self.config)
        except BaseException:
            self.close()
            raise

    def request(self, command, **args):
        stage = {"identity": "qq.verify_identity", "prepare_text": "qq.prepare_text",
                 "submit_text": "qq.send_and_receipt", "prepare_voice": "qq.prepare_voice",
                 "prepare_image": "qq.prepare_image", "prepare_file": "qq.prepare_file"}.get(command, "qq.transport")
        with timing_span(stage):
            return self._request(command, **args)

    def _request(self, command, **args):
        with self._lock:
            if self.closed or self.process is None or self.process.poll() is not None:
                raise AssistantError("qq_native_disconnected", "QQ 后台连接已结束")
            self.sequence += 1
            identifier = self.sequence
            wire = json.dumps({"id": identifier, "command": command, "args": args}, ensure_ascii=False).encode("utf-8") + b"\n"
            limit = 14 * 1024 * 1024 if command in {'prepare_image', 'prepare_file'} else (2 * 1024 * 1024 if command == 'prepare_voice' else 262144)
            if len(wire) > limit:
                raise AssistantError("qq_native_request_limit", "QQ 后台请求超过限制")
            result = queue.Queue(maxsize=1)
            statistics = {"discarded_bytes": 0, "max_line_bytes": 0}

            def exchange():
                reason = "pipe_write_failed"
                try:
                    pending = memoryview(wire)
                    while pending:
                        count = self.process.stdin.write(pending)
                        if not count:
                            raise OSError()
                        pending = pending[count:]
                    self.process.stdin.flush()
                    discarded = 0
                    while discarded <= 20 * 1024 * 1024:
                        reason = "pipe_read_failed"
                        line = self.process.stdout.readline(self.MAX_REPLY + 1)
                        statistics["max_line_bytes"] = max(statistics["max_line_bytes"], len(line))
                        if not line:
                            reason = "pipe_eof"
                            raise ValueError()
                        if len(line) > self.MAX_REPLY:
                            reason = "line_limit"
                            raise ValueError()
                        if line.startswith(self.prefix):
                            reason = "invalid_json"
                            value = json.loads(line[len(self.prefix):])
                            if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != identifier:
                                reason = "reply_identity"
                                raise ValueError()
                            result.put((value, None))
                            return
                        discarded += len(line)
                        statistics["discarded_bytes"] = discarded
                    reason = "diagnostic_limit"
                    raise ValueError()
                except Exception:
                    result.put((None, reason))

            thread = threading.Thread(target=exchange, daemon=True, name="qq-native-private-pipe")
            thread.start()
            try:
                value, failure = result.get(timeout=self.timeout)
            except queue.Empty:
                self._record_failure("timeout", identifier, statistics)
                self.close()
                raise AssistantError("qq_native_worker_timeout", "QQ 后台操作未在限定时间内完成") from None
            if value is None:
                self._record_failure("cancelled" if self.closed else failure, identifier, statistics)
                self.close()
                raise AssistantError("qq_native_protocol_invalid", "QQ 后台通信中断或格式无效")
            if self.closed:
                raise AssistantError("qq_native_disconnected", "QQ 后台连接已结束")
            if value.get("ok") is True and set(value) == {"id", "ok", "result"}:
                return value["result"]
            code = value.get("error")
            if (value.get("ok") is False and set(value) == {"id", "ok", "error"}
                    and isinstance(code, str) and code in self.ERRORS):
                raise AssistantError(code, "QQ 后台操作未完成，请检查账号与连接状态")
            self._record_failure("reply_shape", identifier, statistics)
            self.close()
            raise AssistantError("qq_native_protocol_invalid", "QQ 后台通信格式无效")

    def _record_failure(self, reason, identifier, statistics):
        # Do not retain command arguments, frames, native logs, accounts or text.
        self.last_failure = {"reason": reason, "request_id": identifier, **statistics,
            "exit_code_before_cleanup": self.process.poll() if self.process is not None else None}

    def close(self):
        # A stop must not wait for request()'s potentially long response deadline.
        with self._lifecycle_lock:
            self.closed = True
            try:
                if self.scope is not None:
                    self.scope.close()
                    self.scope = None
            finally:
                if self.process is not None:
                    for pipe in (self.process.stdin, self.process.stdout):
                        if pipe is not None:
                            pipe.close()
            if self.media_stage is not None and (self.process is None or self.process.poll() is not None):
                self.media_stage.cleanup()
