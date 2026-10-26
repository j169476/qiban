"""Lazy service adapter. Construction/status never open WeChat or start workers."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from companion_core.contracts import ActionResult, Capability, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyScope, ReplyTarget
from companion_core.schema import obj

from .collector import collector_account
from .contracts import require
from .db_cli import load_config
from .db_source import validate_enrollment
from .gateway import PermitAuthority
from .native_sender import NativeDriver, NativeSenderGateway
from .receipt_client import DatabaseReceiptClient
from .receiver import DatabaseGateway
from .receiver_transport import SubprocessTransport, WorkerError
from .role import RoleAdapter
from .session import RuntimeSession


def load_options(project):
    path = Path(project) / ".dev/wechat/module.json"
    if not path.exists():
        return None
    require(path.stat().st_size < 16000, "wechat_module_configuration")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), "wechat_module_configuration")
    fields = {"version", "locations", "targets", "database_python", "native_dll"}
    require((value.get("version") == 1 and set(value) == fields | {"family_enabled"}
             and type(value["family_enabled"]) is bool)
            or (value.get("version") == 2 and set(value) == fields), "wechat_module_configuration")
    # Legacy family_enabled is configuration history, never an authorization.
    for key in ("locations", "targets", "database_python", "native_dll"):
        require(isinstance(value[key], str) and 0 < len(value[key]) < 4096, "wechat_module_configuration")
        target = Path(value[key])
        value[key] = target if target.is_absolute() else (Path(project) / target).resolve()
    return value


class ReconnectingTransport:
    """Every source open resolves the enrolled account again, ignoring stale PIDs."""
    def __init__(self, python, *, select=None, worker=None, check=None, clock=time.monotonic):
        self.worker = worker or SubprocessTransport(python=python)
        self.select = select or self._select
        self.check, self.clock = check or self._check_client, clock
        self.identity = self.account = None
        self.next_check = 0

    @staticmethod
    def _check_client(account, identity):
        from .db_windows import READ_ACCESS
        from .native.protocol import ProbeFailure
        from .native.windows import Windows

        windows = Windows()
        pid, born = identity
        handle = None
        try:
            handle = windows.api.open(pid, READ_ACCESS)
            if windows.born(handle) != born:
                raise WorkerError("process_closed")
            core = windows.modules(pid).get("weixin.dll")
            if core is None or windows.account(handle, core[0]) != account:
                raise WorkerError("client_identity_changed")
        except ProbeFailure as exc:
            raise WorkerError("client_not_running" if str(exc) in {"account_not_logged_in", "account_unavailable"}
                              else "process_closed") from None
        except OSError:
            raise WorkerError("process_access_denied") from None
        finally:
            if handle is not None:
                windows.api.close(handle)

    @staticmethod
    def _select(account):
        from .native.protocol import ProbeFailure
        from .native.windows import Windows

        try:
            return Windows().select(account)
        except ProbeFailure as exc:
            code = str(exc)
            raise WorkerError("client_version_unsupported" if code.startswith("unsupported_")
                              else "client_not_running") from None
        except Exception:
            raise WorkerError("process_access_denied") from None

    async def request(self, command, **arguments):
        if command == "open":
            enrollment = arguments.get("enrollment")
            require(isinstance(enrollment, dict), "wechat_module_enrollment_required")
            self.account = enrollment["self_username"]
            self.identity = await asyncio.to_thread(self.select, self.account)
            pid, _born = self.identity
            arguments["config"] = {**arguments["config"], "client_pid": pid}
            self.next_check = self.clock() + 5
        elif command == "read" and self.clock() >= self.next_check:
            require(self.identity is not None, "wechat_module_enrollment_required")
            await asyncio.to_thread(self.check, self.account, self.identity)
            self.next_check = self.clock() + 5
        return await self.worker.request(command, **arguments)

    async def close(self):
        await self.worker.close()


class ContinuousAdapter(RoleAdapter):
    manages_action_quota = True
    def __init__(self, *args):
        super().__init__(*args)
        self.timeout = 30.0

    def capabilities(self):
        return [Capability("message.reply", "向当前授权微信会话回复单行纯文本，最多300字符和1023个UTF-8字节", obj({
            "text": {"type": "string", "minLength": 1, "maxLength": 300, "pattern": r"^[^\r\n]+$"}})),
            Capability("conversation.end", "仅群聊：话题结束或转向无关讨论时，停止本轮参与，等待再次@或引用。不会发消息。", obj({}))]

    @asynccontextmanager
    async def decision_material(self, event, *, guard):
        guard()
        self.session.check_event(event)
        record, run = self.session._active()
        yield {"context": self.session.participation.context(record["admissions"][event.id]["ticket"], run["generation"]),
               "images": ()}

    async def execute(self, capability, arguments, *, guard):
        event = Event.from_dict(arguments["_event"])
        with self.store.lock:
            guard()
            self.session.check_event(event)
            record, run = self.session._active()
            key = stable_key("dispatch", "wechat", self.account, event.id, capability)
            with self.store.db() as db:
                row = db.execute("SELECT id,status FROM core_actions WHERE dedupe=?", (key,)).fetchone()
            require(row is not None and row["status"] == "dispatching", "wechat_action_missing")
            action_id = row["id"]
            if capability == "conversation.end":
                self.session.participation.end(record["admissions"][event.id]["ticket"], run["generation"])
                return ActionResult("succeeded", {"participation_ended": True})
        result = await super().execute(capability, arguments, guard=guard)
        self.session.participation.settle(action_id, result.status)
        return result

    def validate_run(self, run):
        super().validate_run(run)
        require(not self.sender.driver.tainted, "wechat_native_unconfirmed")

    async def collect_once(self):
        incoming = await super().collect_once()
        # Drain the durable queue while offline, but preserve admitted replies
        # until the source can provide a fresh send receipt watermark again.
        return incoming if self.receiver.status()["state"] == "connected" else []


class WeChatModule:
    platform = "wechat"
    interactive = True
    poll_interval_seconds = 1
    manages_action_quota = True

    def __init__(self, store, project, installation_id, *, options=None, factory=None):
        self.store, self.project = store, Path(project)
        self.account = collector_account(installation_id)
        self.options = options
        self.factory = factory or self._compose
        self.core = self.delegate = None
        self.stopping = False

    def bind_runtime(self, core):
        self.core = core

    def reply_targets(self):
        pointer = self.store.state("wechat-db-enrollment:" + stable_key("logical-account", self.account))
        if pointer is None:
            return ()
        enrollment = validate_enrollment(pointer["enrollment"])
        return tuple(ReplyTarget(ReplyScope("wechat", enrollment["account_id"], target["kind"],
            target["username"]), target["username"], enrollment["self_username"],
            "私聊直接回应；群聊由真实@或引用唤起，已核验成员可在参与阶段交流")
            for target in enrollment["targets"].values())

    def _compose(self):
        from .native.bridge import NativeBridge, validate_build

        options = self.options or load_options(self.project)
        require(options is not None, "wechat_module_configuration")
        require(options["database_python"].is_file(), "wechat_database_python_missing")
        config = load_config(options["locations"], options["targets"])
        receiver = DatabaseGateway(self.store, config, account=self.account,
            transport_factory=lambda: ReconnectingTransport(options["database_python"]))
        pointer = self.store.state(receiver.pointer_key)
        require(pointer is not None, "wechat_module_enrollment_required")
        receiver._initialize(pointer)  # Encrypted metadata only. pump_once opens the source lazily.
        enrollment = receiver._saved["enrollment"]
        targets = {alias: target["username"] for alias, target in enrollment["targets"].items()}
        session = RuntimeSession(self.store, self.account, family=True)
        permits = PermitAuthority(secrets.token_bytes(32))
        driver = NativeDriver(lambda: NativeBridge(options["native_dll"], enrollment["self_username"], targets),
                              DatabaseReceiptClient(receiver), preflight=lambda: validate_build(options["native_dll"]))
        sender = NativeSenderGateway(self.store, receiver, permits, driver, session, timeout=25)
        return ContinuousAdapter(receiver, sender, permits, session)

    def validate_start(self):
        from .native.protocol import ProbeFailure

        require(not self.stopping and self.delegate is None, "wechat_module_stopping")
        delegate = None
        try:
            delegate = self.factory()
            delegate.bind_runtime(self.core)
            delegate.validate_start()
        except BaseException as exc:
            if delegate is not None:
                delegate.session.close()  # Preflight does not open workers or native processes.
            if isinstance(exc, (ProbeFailure, OSError, ValueError)):
                messages = {
                    "bridge_build_changed": "微信桥接构建与当前源码不一致，请在接入向导重新准备微信连接依赖。",
                    "bridge_build_missing": "微信桥接构建或清单缺失，请在接入向导准备微信连接依赖。",
                    "bridge_build_invalid": "微信桥接构建或清单无法读取，请在接入向导重新准备微信连接依赖。",
                    "invalid_bridge": "微信桥接文件无效，请在接入向导重新准备微信连接依赖。",
                    "client_restart_required": "微信客户端需要重启，请正常退出并重开对应账号的客户端。",
                }
                code = str(exc) if isinstance(exc, ProbeFailure) and str(exc) in messages else "wechat_native_preflight_failed"
                raise AssistantError(code, messages.get(code, "微信原生预检未通过，请检查接入配置和桥接构建。")) from None
            raise
        self.delegate = delegate

    def validate_run(self, run):
        require(self.delegate is not None and not self.stopping, "wechat_module_not_started")
        self.delegate.validate_run(run)

    def capabilities(self):
        return ContinuousAdapter.capabilities(self)

    def validate_event(self, event):
        require(self.delegate is not None and not self.stopping, "wechat_module_not_started")
        return self.delegate.validate_event(event)

    async def receive(self, cursor):
        return await self.delegate.receive(cursor)

    async def runtime_poll(self):
        return await self.delegate.collect_once()

    def workflow(self, core):
        return self.delegate.workflow(core)

    @asynccontextmanager
    async def decision_material(self, event, *, guard):
        async with self.delegate.decision_material(event, guard=guard) as material:
            yield material

    async def read(self, reference):
        raise AssistantError("wechat_read_unsupported", "微信模块不提供任意聊天资料读取")

    async def execute(self, *args, **kwargs):
        require(self.delegate is not None and not self.stopping, "wechat_module_not_started")
        return await self.delegate.execute(*args, **kwargs)

    def runtime_stopped(self, generation):
        if self.delegate is not None:
            self.stopping = True
            self.delegate.runtime_stopped(generation)

    async def idle(self):
        # The watchdog can pause the encrypted run from a different process,
        # without invoking runtime_stopped on this Python object.
        if self.delegate is not None:
            await self.close()

    async def close(self):
        self.stopping = True
        delegate, self.delegate = self.delegate, None
        try:
            if delegate is not None:
                await delegate.close()
        finally:
            self.stopping = False

    def runtime_status(self):
        if self.delegate is None:
            return {"state": "paused", "automatic_start": False}
        driver = self.delegate.sender.driver
        return {"state": "stopping" if self.stopping else "running",
                "receiver": self.delegate.receiver.status(), "session": self.delegate.session.public_status(),
                "native_uncertain": driver.tainted,
                "native": driver.public_status() if hasattr(driver, "public_status") else {}, "automatic_start": False}


def configured_module(store, project, installation_id):
    # Presence registers the connector; malformed configuration is reported on
    # explicit WeChat start and cannot take Bilibili or the local room offline.
    return WeChatModule(store, project, installation_id) if (Path(project) / ".dev/wechat/module.json").exists() else None
