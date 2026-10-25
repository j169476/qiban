"""Trusted bridge contracts, independent of any particular desktop automation library."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from companion_core.errors import AssistantError


def require(condition, code="wechat_invalid_input"):
    if not condition:
        # Never echo a provider error, message, account or UI title into diagnostics.
        raise AssistantError(code, "微信边界校验未通过，请检查连接状态与绑定")


def identifier(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Connection:
    account_identity: str
    epoch: str
    ready: bool = True


@dataclass(frozen=True)
class Observation:
    """An immutable observation, NOT a UI runtime ID or a content hash.

    The producer allocates and retains observation_id once, before delivery. It
    must baseline old visible messages and must never mark ambiguous history new.
    P1 accepts a verified read-only database producer or a synthetic gateway;
    diagnostic P0 probes never produce core events.
    """

    observation_id: str
    account_identity: str
    epoch: str
    target_ref: str
    kind: str
    occurred_at: str
    text: str
    sender_ref: str = ""
    direction: str = "incoming"
    trigger: str = "none"
    time_basis: str = "observed"

    def __post_init__(self):
        require(isinstance(self.observation_id, str)
                and re.fullmatch(r"[a-f0-9]{32}", self.observation_id) is not None)
        require(all(identifier(v) for v in (self.account_identity, self.epoch, self.target_ref)))
        require(self.kind in ("private", "group"))
        require(isinstance(self.text, str) and len(self.text) <= 12000)
        require(isinstance(self.sender_ref, str) and len(self.sender_ref) <= 200)
        require(self.direction in ("incoming", "self", "system", "history"))
        require(self.trigger in ("none", "direct", "mention", "reply"))
        require(self.time_basis in ("observed", "server"))
        try:
            stamp = datetime.fromisoformat(self.occurred_at)
            require(stamp.tzinfo is not None and stamp.utcoffset() is not None)
        except (ValueError, TypeError):
            require(False)

    @property
    def fingerprint(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class Batch:
    observations: tuple[Observation, ...]
    cursor: dict


@dataclass(frozen=True)
class SendRequest:
    action_id: str
    generation: str
    account_identity: str
    epoch: str
    conversation: str
    revision: int
    target_ref: str
    text: str

    @property
    def fingerprint(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class PreparedSend:
    ticket: str
    request_digest: str


@dataclass(frozen=True)
class SendPermit:
    ticket: str
    request_digest: str
    expires_at: float
    signature: str
