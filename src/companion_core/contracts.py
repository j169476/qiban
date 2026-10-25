"""Versioned, platform-neutral boundary objects. Native IDs are always scoped."""
import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Protocol

from .errors import AssistantError


def stable_key(kind, *parts):
    return kind + "-" + sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:40]


@dataclass(frozen=True)
class ActorRef:
    platform: str
    user_id: str
    scope: str = ""

    def __post_init__(self):
        if not self.platform or not self.user_id or any(not isinstance(v, str) or len(v) > 200
                                                       for v in (self.platform, self.user_id, self.scope)):
            raise AssistantError("invalid_actor", "主体需要平台、作用域和原始标识")

    @property
    def key(self):
        return stable_key("actor", self.platform, self.scope, self.user_id)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Event:
    platform: str
    account: str
    native_id: str
    actor: ActorRef
    conversation: str
    occurred_at: str
    text: str
    visibility: str = "private"
    attachments: tuple = ()
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.actor.platform != self.platform or not all((self.platform, self.account, self.native_id, self.conversation)):
            raise AssistantError("invalid_event", "事件平台与发送者不匹配或缺少作用域")
        if self.visibility not in ("private", "group", "public") or len(self.text) > 64000:
            raise AssistantError("invalid_event", "事件可见范围或内容长度无效")

    @property
    def id(self):
        return stable_key("event", self.platform, self.account, self.native_id)

    @property
    def thread(self):
        return stable_key("conversation", self.platform, self.account, self.conversation)

    def as_dict(self):
        return {**asdict(self), "id": self.id, "thread": self.thread}

    @classmethod
    def from_dict(cls, data):
        values = {k: v for k, v in data.items() if k not in ("id", "thread")}
        values["actor"] = ActorRef(**values["actor"])
        values["attachments"] = tuple(values.get("attachments", ()))
        return cls(**values)


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    parameters: dict
    effect: str = "write"

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ActionResult:
    status: str
    receipt: dict = field(default_factory=dict)
    error: str = ""

    def __post_init__(self):
        if self.status not in ("succeeded", "failed", "uncertain", "cancelled"):
            raise AssistantError("invalid_result", "无效的动作执行结果")


class PlatformPort(Protocol):
    platform: str
    account: str

    def capabilities(self) -> list[Capability]: ...
    async def receive(self, cursor: dict) -> tuple[list[Event], dict]: ...
    async def read(self, reference: dict) -> dict: ...
    async def execute(self, capability: str, arguments: dict, *, guard) -> ActionResult: ...


class ModelPort(Protocol):
    async def generate(self, instructions, context, output_schema, images=(), **options): ...


class HostPort(Protocol):
    def identity(self) -> str: ...
    def require_active(self, thread_id=None): ...


class PlatformRegistry:
    def __init__(self, adapters=()):
        self._adapters = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter):
        key = adapter.platform, adapter.account
        if key in self._adapters:
            raise AssistantError("duplicate_account", "同一平台账号已注册")
        capabilities = adapter.capabilities()
        if len({c.name for c in capabilities}) != len(capabilities):
            raise AssistantError("duplicate_capability", "平台能力名称重复")
        self._adapters[key] = adapter

    def get(self, platform, account):
        try:
            return self._adapters[platform, account]
        except KeyError:
            raise AssistantError("platform_missing", "该平台账号尚未接入") from None

    def all(self):
        return list(self._adapters.values())
