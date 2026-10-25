"""Native text driver composed with the encrypted, at-most-once send journal."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

from .contracts import identifier, require
from .live_sender import DriverOutcome, LiveSenderGateway


@dataclass(frozen=True, repr=False)
class NativeProof:
    account_identity: str
    target_ref: str
    client_epoch: str
    request_digest: str
    sequence: int
    ready: bool = False


class NativeSenderGateway(LiveSenderGateway):
    @staticmethod
    def _request_valid(request):
        LiveSenderGateway._request_valid(request)
        require(len(request.text.encode("utf-8")) <= 1023 and len(request.text) <= 300,
                "wechat_send_text_invalid")

    @staticmethod
    def _proof(request, proof):
        require(isinstance(proof, NativeProof) and proof.ready is True
                and proof.account_identity == request.account_identity and proof.target_ref == request.target_ref
                and proof.request_digest == request.fingerprint and identifier(proof.client_epoch)
                and proof.client_epoch != request.epoch and type(proof.sequence) is int and proof.sequence > 0,
                "wechat_send_target_unverified")
        return proof

    def preflight(self):
        super().preflight()
        check = getattr(self.driver, "preflight", None)
        if check:
            check()  # Files/configuration only; no client access.

    def revoke_generation(self, generation):
        try:
            super().revoke_generation(generation)
        finally:
            self.driver.revoke()


class NativeDriver:
    def __init__(self, backend_factory, receipts, *, preflight=lambda: None,
                 settle_seconds=3.0, confirmation_seconds=20.0, clock=time.monotonic, sleep=asyncio.sleep):
        require(3 <= settle_seconds <= 10 and 0 < confirmation_seconds <= 20, "wechat_native_configuration")
        self.factory, self.receipts, self.preflight = backend_factory, receipts, preflight
        self.settle, self.confirmation, self.clock, self.sleep = settle_seconds, confirmation_seconds, clock, sleep
        self.backend = self.token = self.request = self.proof = None
        self.revoked = self.tainted = False
        self.last_code = ""
        self._native_lock = threading.RLock()

    def _call(self, method, *args):
        with self._native_lock:
            try:
                return getattr(self.backend, method)(*args)
            except Exception as exc:
                from .native.protocol import ProbeFailure

                # Only constant codes from local code; no Windows exception data.
                self.last_code = str(exc) if isinstance(exc, ProbeFailure) else "native_call_failed"
                raise

    async def _cleanup(self):
        if self.token is not None:
            await self.receipts.discard(self.request, self.token)
        self.token = self.request = self.proof = None

    async def prepare(self, request):
        require(not self.revoked and not self.tainted, "wechat_native_closed")
        NativeSenderGateway._request_valid(request)
        await self._cleanup()
        if self.backend is not None:
            try:
                self._call("check")
            except Exception:
                require(not self.backend.pending, "wechat_native_closed")
                try:
                    self._call("close")
                except Exception:
                    pass
                self.backend = None
        if self.backend is None:
            self.backend = self.factory()
            # Native calls are bounded. Keep this on the owning event-loop thread:
            # cancellation must not leave a worker continuing to prepare/send.
            try:
                self._call("open")
            except BaseException:
                self.tainted = bool(self.backend.pending)
                try:
                    self._call("close")
                except Exception:
                    pass
                self.backend = None
                raise
        try:
            self.request = request
            self.token = await self.receipts.begin(request)
            deadline = self.clock() + self.settle
            while self.clock() < deadline:
                require(not self.revoked, "core_before_send_cancelled")
                await self.sleep(max(0.001, deadline - self.clock()))
            require(not self.revoked, "core_before_send_cancelled")
            self._call("prepare", request)
            self.proof = NativeProof(request.account_identity, request.target_ref, self.backend.epoch,
                                     request.fingerprint, self.backend.sequence, True)
            return self.inspect(request)
        except BaseException:
            await self._cleanup()
            raise

    def inspect(self, request):
        require(not self.revoked and not self.tainted and self.proof is not None
                and self.proof.request_digest == request.fingerprint, "wechat_native_closed")
        self._call("inspect")
        return self.proof

    def submit_once(self, request, proof):
        require(proof == self.inspect(request), "wechat_send_target_changed")
        try:
            self._call("submit_once")
        except BaseException:
            self.tainted = True  # Unknown native completion cannot be retried.
            raise
        return DriverOutcome("uncertain")

    async def confirm(self, request, proof, _initial):
        require(proof == self.proof and request == self.request, "wechat_native_scope")
        recorded = False
        deadline = self.clock() + self.confirmation
        try:
            while not self.revoked and self.clock() < deadline:
                status = self._call("status")
                require(status["sequence"] == proof.sequence, "wechat_native_scope")
                if not recorded:
                    outcome = await self.receipts.confirm(request, self.token)
                    recorded = outcome.status == "confirmed" and outcome.client_recorded
                # A receipt handle is consumed on success: latch it, never query again.
                if (recorded and status["completed"] == 1 and not status["error"]
                        and not any(status[k] for k in ("tasks", "completions", "callbacks"))):
                    return DriverOutcome("confirmed", True)
                if status["error"] or status["completed"] == 2:
                    break
                await self.sleep(0.25)
            self.tainted = True
            return DriverOutcome("uncertain")
        except BaseException:
            self.tainted = True
            raise
        finally:
            await self._cleanup()

    async def discard(self, proof):
        if self.proof == proof:
            try:
                self._call("cancel")
            finally:
                await self._cleanup()

    def revoke(self):
        self.revoked = True
        if self.backend is not None:
            try:
                self._call("cancel")
            except Exception:
                self.tainted = True

    async def close(self):
        self.revoke()
        try:
            await self._cleanup()
        finally:
            if self.backend is not None:
                self._call("close")

    def public_status(self):
        return {"connected": self.backend is not None, "uncertain": self.tainted,
                "revoked": self.revoked, "code": self.last_code}

