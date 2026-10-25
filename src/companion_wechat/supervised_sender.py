"""One supervised desktop submission over a private operator protocol.

The operator is trusted application code, not a model-selected tool. Python never
operates the desktop here. A claim may grant exactly one immediately executed
Sky action; an issued grant is already an uncertain external effect until the
read-only receipt confirms it. Neither cancellation nor reconnect reissues it.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import time
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from companion_core.contracts import ActionResult
from companion_core.errors import AssistantError

from .contracts import PreparedSend, SendPermit, SendRequest, digest, identifier, require
from .gateway import PermitAuthority
from .live_sender import DriverOutcome, LiveSenderGateway, _sync


@dataclass(frozen=True, repr=False)
class StagedProof:
    """Trusted operator observation of the exact staged, unsent draft.

    This is deliberately distinct from an empty-draft native SendProof. The
    native account and target must have been verified by the supervising agent;
    copied config fields, nicknames or window titles alone are not proof.
    view_token identifies the final observed draft before the one-action cell.
    """

    account_identity: str
    target_ref: str
    client_epoch: str
    view_token: str
    text_digest: str
    observed: bool = False
    draft_matches: bool = False
    manual_takeover: bool = True


class SupervisedOperator(Protocol):
    async def prepare(self, request: SendRequest) -> StagedProof: ...

    async def dispatch_once(self, request: SendRequest, proof: StagedProof, *, authorize) -> DriverOutcome:
        """Wait for claim, call authorize synchronously, then emit at most one grant.

        authorize is an in-process callback, never serialized or acknowledged
        later. After success, grant delivery failure is an unknown submission.
        Parent 'submitted' only reports an attempt; confirmed requires the
        independently scoped, post-watermark database receipt.
        """
        ...


class SupervisedSenderGateway:
    simulation = False

    # Share action/ticket namespaces and historical semantics with native live
    # sending: changing driver type must not create a second attempt allowance.
    _action_key = staticmethod(LiveSenderGateway._action_key)
    _ticket_key = staticmethod(LiveSenderGateway._ticket_key)
    _revocation_key = staticmethod(LiveSenderGateway._revocation_key)
    _historical = staticmethod(LiveSenderGateway._historical)
    _ticket_result = LiveSenderGateway._ticket_result
    _finish = LiveSenderGateway._finish
    _outcome = staticmethod(LiveSenderGateway._outcome)

    def __init__(self, store, receiver, permits, operator, session, *,
                 prepare_timeout=180.0, commit_timeout=20.0, clock=time.monotonic):
        require(type(prepare_timeout) in (int, float) and math.isfinite(prepare_timeout)
                and 0 < prepare_timeout <= 180, "wechat_supervised_configuration")
        require(type(commit_timeout) in (int, float) and math.isfinite(commit_timeout)
                and 0 < commit_timeout <= 30, "wechat_supervised_configuration")
        self.store, self.receiver, self.permits = store, receiver, permits
        self.operator, self.session = operator, session
        self.prepare_timeout, self.commit_timeout, self.clock = prepare_timeout, commit_timeout, clock
        self.send_session = uuid.uuid4().hex
        self._pending, self._revoked = {}, set()
        self._commit_lock = asyncio.Lock()
        self._closed = False

    @property
    def available(self):
        return self.operator is not None

    def preflight(self):
        require(self.available, "wechat_live_driver_missing")
        require(all(callable(getattr(self.operator, name, None)) for name in
                    ("prepare", "dispatch_once")), "wechat_supervised_operator_invalid")
        require(isinstance(self.permits, PermitAuthority)
                and all(callable(getattr(self.session, name, None)) for name in
                        ("check_request", "reserve")), "wechat_live_session_invalid")
        require(self.store.ledger_encode("wechat-live-preflight", {"version": 1}).startswith("enc1:"),
                "wechat_send_store_unencrypted")

    def _check(self, request):
        self.preflight()
        LiveSenderGateway._request_valid(request)
        require(not self._closed and request.generation not in self._revoked
                and not self.store.state(self._revocation_key(request.generation), False),
                "core_before_send_cancelled")
        connection = self.receiver.connection()
        require((connection.account_identity, connection.epoch) == (request.account_identity, request.epoch),
                "wechat_account_mismatch")
        binding = self.receiver.bindings.get(request.conversation)
        require(binding.enabled and binding.target_ref == request.target_ref and binding.revision == request.revision,
                "wechat_binding_changed")
        _sync(self.session.check_request(request))

    @staticmethod
    def _proof(request, proof):
        require(isinstance(proof, StagedProof) and proof.observed is True and proof.draft_matches is True
                and proof.manual_takeover is False, "wechat_send_target_unverified")
        require(proof.account_identity == request.account_identity and proof.target_ref == request.target_ref
                and proof.text_digest == digest(request.text), "wechat_send_target_unverified")
        require(identifier(proof.client_epoch) and identifier(proof.view_token)
                and proof.client_epoch != request.epoch, "wechat_send_target_unverified")

    async def prepare(self, request):
        self._check(request)
        require(len(self._pending) < 2, "wechat_queue_full")
        previous = self.store.state(self._action_key(request))
        if previous is not None:
            require(previous.get("request_digest") == request.fingerprint, "wechat_action_conflict")
            proof = None
        else:
            try:
                proof = await asyncio.wait_for(self.operator.prepare(request), self.prepare_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise AssistantError("wechat_supervised_prepare_failed", "受监督发送准备未完成") from None
            self._check(request)
            self._proof(request, proof)
        ticket = uuid.uuid4().hex
        self._pending[ticket] = (request, proof, self.clock())
        return PreparedSend(ticket, request.fingerprint)

    def _claim(self, request, proof, permit):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self.store.state(self._action_key(request), db=db)
            if previous is not None:
                return self._historical(previous, request.fingerprint)
            _sync(self.session.reserve(request, db=db))
            self.store.set_state(self._action_key(request), {
                "request_digest": request.fingerprint, "status": "submitting", "result": None,
                "proof_digest": digest(asdict(proof)), "send_session": self.send_session,
                "transport": "supervised", "grant_issued": False,
            }, db)
            self.store.set_state(self._ticket_key(permit.ticket), {
                "permit": asdict(permit), "action_key": self._action_key(request), "result": None,
            }, db)
        return None

    def _grant(self, request, proof, permit):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.store.state(self._action_key(request), db=db)
            require(isinstance(record, dict) and record.get("request_digest") == request.fingerprint
                    and record.get("proof_digest") == digest(asdict(proof))
                    and record.get("status") == "submitting" and record.get("send_session") == self.send_session
                    and record.get("grant_issued") is False, "wechat_supervised_grant_invalid")
            _sync(self.session.check_request(request, db=db))
            self.permits.verify(permit, request)
            self.store.set_state(self._action_key(request), {**record, "grant_issued": True}, db)

    async def commit(self, permit, *, guard):
        if not isinstance(permit, SendPermit):
            return ActionResult("cancelled", error="wechat_permit_invalid")
        async with self._commit_lock:
            try:
                prior = self._ticket_result(permit)
            except Exception:
                return ActionResult("uncertain", error="wechat_send_journal_failed")
            if prior is not None:
                return prior
            pending = self._pending.pop(permit.ticket, None)
            if pending is None:
                return ActionResult("uncertain", error="wechat_ticket_unknown")
            request, proof, staged_at = pending
            claimed = granted = False
            try:
                self.permits.verify(permit, request)
                previous = self.store.state(self._action_key(request))
                if previous is not None:
                    return self._finish(request, permit, self._historical(previous, request.fingerprint), claimed=False)
                _sync(guard())
                self._check(request)
                self._proof(request, proof)
                self.permits.verify(permit, request)
                prior = self._claim(request, proof, permit)
                if prior is not None:
                    return self._finish(request, permit, prior, claimed=False)
                claimed = True

                def authorize():
                    nonlocal granted
                    require(not granted, "wechat_supervised_grant_invalid")
                    task = asyncio.current_task()
                    require(task is None or not task.cancelling(), "core_before_send_cancelled")
                    _sync(guard())
                    self._check(request)
                    self._proof(request, proof)
                    require(0 <= self.clock() - staged_at <= 30, "wechat_supervised_proof_expired")
                    self.permits.verify(permit, request)
                    self._grant(request, proof, permit)
                    self.permits.verify(permit, request)
                    # From this linearization point, a grant may reach the UI.
                    # Cancellation cannot assert that no external action ran.
                    granted = True
                    return {"expires_unix_ms": int((time.time() + min(2.0, max(
                        0.0, permit.expires_at - self.permits.clock()))) * 1000)}

                outcome = self._outcome(await asyncio.wait_for(
                    self.operator.dispatch_once(request, proof, authorize=authorize), self.commit_timeout))
                require(granted, "wechat_supervised_grant_missing")
                if outcome.status == "confirmed":
                    result = ActionResult("succeeded", {"client_recorded": True, "action_id": request.action_id})
                else:
                    # Even a rejected/missing operator report after grant cannot
                    # prove the separately executing Sky action had no effect.
                    result = ActionResult("uncertain", error="wechat_send_unconfirmed")
            except asyncio.CancelledError:
                result = ActionResult("uncertain" if granted else "cancelled", error="wechat_task_cancelled")
            except AssistantError as exc:
                result = ActionResult("uncertain" if granted else "cancelled",
                                      error="wechat_send_unconfirmed" if granted else exc.code)
            except Exception:
                result = ActionResult("uncertain" if granted else "cancelled",
                                      error="wechat_send_unconfirmed" if granted else "wechat_supervised_failed")
            return self._finish(request, permit, result, claimed=claimed)

    async def discard(self, ticket):
        pending = self._pending.pop(ticket, None)
        callback = getattr(self.operator, "discard", None)
        if pending is not None and pending[1] is not None and callable(callback):
            try:
                value = callback(pending[1])
                if inspect.isawaitable(value):
                    await asyncio.wait_for(value, self.commit_timeout)
            except Exception:
                pass

    def revoke_generation(self, generation):
        if generation is None:
            return
        require(identifier(generation), "wechat_send_request_invalid")
        self._revoked.add(generation)
        try:
            callback = getattr(self.session, "revoke_generation", None)
            if callable(callback):
                _sync(callback(generation))
            self.store.set_state(self._revocation_key(generation), True)
        except Exception:
            self._closed = True
            raise AssistantError("wechat_send_revoke_failed", "发送边界已关闭") from None

    def connection(self):
        value = self.receiver.connection()
        return replace(value, ready=False) if self._closed else value

    async def acknowledge(self, cursor):
        return await self.receiver.acknowledge(cursor)

    async def poll(self, cursor, *, limit=100):
        return await self.receiver.poll(cursor, limit=limit)

    async def close(self):
        self._closed = True
        self._pending.clear()
        try:
            callback = getattr(self.operator, "close", None)
            if callable(callback):
                value = callback()
                if inspect.isawaitable(value):
                    await asyncio.wait_for(value, self.commit_timeout)
        finally:
            await self.receiver.close()
