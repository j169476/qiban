"""P1 gateway protocol and single-use send authorization; no network listener."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from typing import Protocol

from companion_core.contracts import ActionResult

from .contracts import Batch, Connection, PreparedSend, SendPermit, SendRequest, require


class PermitAuthority:
    def __init__(self, secret: bytes, *, clock=time.monotonic, lifetime_seconds=2.0):
        require(isinstance(secret, bytes) and len(secret) >= 32)
        require(type(lifetime_seconds) in (int, float) and math.isfinite(lifetime_seconds)
                and 0 < lifetime_seconds <= 20, "wechat_permit_configuration")
        self._secret, self.clock = secret, clock
        self.lifetime_seconds = lifetime_seconds

    def _signature(self, ticket, fingerprint, expires_at):
        body = json.dumps([ticket, fingerprint, expires_at], separators=(",", ":")).encode()
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def issue(self, prepared, request):
        require(prepared.request_digest == request.fingerprint, "wechat_prepared_mismatch")
        deadline = self.clock() + self.lifetime_seconds
        return SendPermit(prepared.ticket, request.fingerprint, deadline,
                          self._signature(prepared.ticket, request.fingerprint, deadline))

    def verify(self, permit, request):
        require(isinstance(permit, SendPermit)
                and permit.request_digest == request.fingerprint
                and self.clock() <= permit.expires_at <= self.clock() + self.lifetime_seconds
                and hmac.compare_digest(permit.signature, self._signature(
                    permit.ticket, permit.request_digest, permit.expires_at)), "wechat_permit_invalid")


class Gateway(Protocol):
    """Implementations authenticate IPC before exposing any of these methods.

    P1 provides FakeGateway and a receive-only DatabaseGateway using private
    parent/child pipes. No localhost listener or live sending implementation is
    supplied or registered with the role service.
    """

    simulation: bool

    def connection(self) -> Connection: ...
    async def acknowledge(self, cursor: dict) -> None: ...
    async def poll(self, cursor: dict, *, limit: int) -> Batch: ...
    async def prepare(self, request: SendRequest) -> PreparedSend: ...
    # P1 is in-process: the fake gateway calls guard after its queue, immediately
    # before the simulated side effect. P2 must design the equivalent authenticated
    # cross-process handshake; serializing this callable is not an IPC protocol.
    async def commit(self, permit: SendPermit, *, guard) -> ActionResult: ...
    async def discard(self, ticket: str) -> None: ...
    def revoke_generation(self, generation: str) -> None: ...
