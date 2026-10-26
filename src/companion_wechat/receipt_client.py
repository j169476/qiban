"""Private, in-memory receipt handles on the existing read-only source worker."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from companion_core.errors import AssistantError

from .contracts import require
from .live_sender import DriverOutcome, LiveSenderGateway
from .receiver_transport import WorkerError


@dataclass(frozen=True, repr=False)
class ReceiptToken:
    handle: str
    request_digest: str
    started: float
    worker: object = field(compare=False)
    owner: object = field(compare=False)


class DatabaseReceiptClient:
    def __init__(self, receiver, *, clock=time.monotonic):
        self.receiver, self.clock = receiver, clock

    def _worker(self, request):
        LiveSenderGateway._request_valid(request)
        receiver = self.receiver
        require(not receiver._closed and receiver._worker is not None and receiver.bindings is not None,
                "wechat_receipt_disconnected")
        connection = receiver.connection()
        require((connection.account_identity, connection.epoch) == (request.account_identity, request.epoch),
                "wechat_account_mismatch")
        binding = receiver.bindings.get(request.conversation)
        require(binding.key == request.conversation and binding.enabled
                and binding.target_ref == request.target_ref and binding.revision == request.revision,
                "wechat_binding_changed")
        return receiver._worker

    async def _call(self, worker, command, **arguments):
        try:
            result = await worker.request(command, **arguments)
            require(not self.receiver._closed and self.receiver._worker is worker, "wechat_receipt_disconnected")
            return result
        except WorkerError as exc:
            # All source-worker failures close its memory key context. Preserve
            # the durable receive checkpoint and use its normal reconnect path.
            if self.receiver._worker is worker:
                await self.receiver._failed(exc)
            raise AssistantError("wechat_receipt_unavailable", "本机发送记录暂无法核验") from None

    async def begin(self, request):
        """Capture actual current source heads, never a saved receive cursor."""
        async with self.receiver._lock:
            worker = self._worker(request)
            result = await self._call(worker, "receipt_begin", action_id=request.action_id,
                                      target_ref=request.target_ref)
            require(isinstance(result, dict) and set(result) == {"handle"}
                    and isinstance(result["handle"], str) and re.fullmatch(r"[a-f0-9]{64}", result["handle"]),
                    "wechat_receipt_invalid")
            return ReceiptToken(result["handle"], request.fingerprint, self.clock(), worker, self)

    def _token(self, request, token):
        require(isinstance(token, ReceiptToken) and token.owner is self
                and token.request_digest == request.fingerprint
                and 0 <= self.clock() - token.started <= 120, "wechat_receipt_invalid")
        worker = self._worker(request)
        require(token.worker is worker, "wechat_receipt_disconnected")
        return worker

    async def confirm(self, request, token):
        """One bounded check. Missing/ambiguous evidence never proves rejection."""
        try:
            async with self.receiver._lock:
                worker = self._token(request, token)
                result = await self._call(worker, "receipt_check", action_id=request.action_id,
                                          handle=token.handle, text=request.text)
                require(isinstance(result, dict) and set(result) == {"status", "confirmed"}
                        and result["status"] in {"client_recorded", "pending", "ambiguous", "uncertain"}
                        and type(result["confirmed"]) is bool
                        and result["confirmed"] == (result["status"] == "client_recorded"),
                        "wechat_receipt_invalid")
                return DriverOutcome("confirmed", True) if result["confirmed"] else DriverOutcome("uncertain")
        except Exception:
            return DriverOutcome("uncertain")

    async def discard(self, request, token):
        try:
            async with self.receiver._lock:
                worker = self._token(request, token)
                await self._call(worker, "receipt_discard", action_id=request.action_id, handle=token.handle)
        except Exception:
            # The worker also expires handles after 120 seconds or at exit.
            pass
