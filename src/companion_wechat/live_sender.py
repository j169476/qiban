"""Trusted live-send boundary; no UI implementation is supplied by this module.

The native driver must independently verify stable account/target identities.
Its synchronous submit_once is the only irreversible operation. It must never
queue a background send or interpret text as keyboard commands. Confirmation
may wait asynchronously, but an ambiguous submission is never retried here.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import unicodedata
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from companion_core.contracts import ActionResult, stable_key
from companion_core.errors import AssistantError

from .contracts import PreparedSend, SendPermit, SendRequest, digest, identifier, require
from .gateway import PermitAuthority


@dataclass(frozen=True, repr=False)
class SendProof:
    """A driver-attested live session, exact UI target, and untouched empty draft.

    client_epoch is a native process/login incarnation, not the durable inbox
    epoch. target_token and draft_token must change after target replacement or
    user interaction; titles/nicknames alone cannot establish these assertions.
    These values are observations from a trusted native driver, never user
    configuration. Distinct epochs alone are not evidence of native liveness.
    Safe defaults deliberately reject an incomplete proof.
    """

    account_identity: str
    target_ref: str
    client_epoch: str
    target_token: str
    draft_token: str
    ready: bool = False
    draft_empty: bool = False
    manual_takeover: bool = True


@dataclass(frozen=True, repr=False)
class DriverOutcome:
    """confirmed means a verified local sent record, never recipient delivery."""

    status: str
    client_recorded: bool = False


class LiveDriver(Protocol):
    async def prepare(self, request: SendRequest) -> SendProof: ...
    def inspect(self, request: SendRequest) -> SendProof: ...
    def submit_once(self, request: SendRequest, proof: SendProof) -> DriverOutcome: ...
    # Optional async confirm(request, proof, initial) may resolve an uncertain
    # submission by a unique post-submission self/target/text receipt. It cannot
    # initiate or repeat a send. Optional discard(proof)/close only clean up.


def _sync(value):
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        require(False, "wechat_driver_contract_invalid")
    return value


class LiveSenderGateway:
    simulation = False
    MAX_TEXT_BYTES = 2048

    def __init__(self, store, receiver, permits, driver, session, *, timeout=5.0):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30)
        self.store, self.receiver, self.permits = store, receiver, permits
        self.driver, self.session, self.timeout = driver, session, timeout
        self.send_session = uuid.uuid4().hex
        self._pending, self._revoked = {}, set()
        self._commit_lock = asyncio.Lock()
        self._closed = False

    @property
    def available(self):
        """Only configuration availability; never inspect or operate the UI."""
        return self.driver is not None

    def preflight(self):
        require(self.available, "wechat_live_driver_missing")
        require(all(callable(getattr(self.driver, name, None)) for name in
                    ("prepare", "inspect", "submit_once")), "wechat_driver_contract_invalid")
        require(isinstance(self.permits, PermitAuthority)
                and all(callable(getattr(self.session, name, None)) for name in
                        ("check_request", "reserve")), "wechat_live_session_invalid")
        require(self.permits.lifetime_seconds <= 2.0, "wechat_permit_configuration")
        # Real sending must never create plaintext operational journals. The
        # host supplies its existing encrypted ledger; this writes no canary.
        require(self.store.ledger_encode("wechat-live-preflight", {"version": 1}).startswith("enc1:"),
                "wechat_send_store_unencrypted")

    @staticmethod
    def _request_valid(request):
        require(isinstance(request, SendRequest), "wechat_send_request_invalid")
        require(all(identifier(getattr(request, name)) for name in
                    ("action_id", "generation", "account_identity", "epoch", "conversation", "target_ref"))
                and type(request.revision) is int and request.revision > 0, "wechat_send_request_invalid")
        try:
            valid = (isinstance(request.text, str) and bool(request.text.strip())
                     and len(request.text.encode("utf-8")) <= LiveSenderGateway.MAX_TEXT_BYTES
                     and all(unicodedata.category(char) not in {"Cc", "Zl", "Zp"} for char in request.text))
        except UnicodeError:
            valid = False
        require(valid, "wechat_send_text_invalid")

    @staticmethod
    def _action_key(request):
        return "wechat-live-action:" + stable_key("send", request.account_identity, request.action_id)

    @staticmethod
    def _ticket_key(ticket):
        return "wechat-live-ticket:" + stable_key("ticket", ticket)

    @staticmethod
    def _revocation_key(generation):
        return "wechat-live-revoked:" + stable_key("generation", generation)

    def _check(self, request):
        self.preflight()
        self._request_valid(request)
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
        require(isinstance(proof, SendProof) and proof.ready is True and proof.draft_empty is True
                and proof.manual_takeover is False, "wechat_send_target_unverified")
        require(proof.account_identity == request.account_identity and proof.target_ref == request.target_ref,
                "wechat_send_target_unverified")
        require(all(identifier(getattr(proof, name)) for name in ("client_epoch", "target_token", "draft_token"))
                and proof.client_epoch != request.epoch, "wechat_send_target_unverified")
        return proof

    def _inspect(self, request, expected):
        try:
            observed = _sync(self.driver.inspect(request))
        except Exception:
            raise AssistantError("wechat_send_target_unverified", "发送目标核验未通过") from None
        self._proof(request, observed)
        require(observed == expected, "wechat_send_target_changed")

    @staticmethod
    def _historical(record, fingerprint):
        if not isinstance(record, dict) or record.get("request_digest") != fingerprint:
            return ActionResult("uncertain", error="wechat_action_conflict")
        if record.get("status") == "finished":
            try:
                return ActionResult(**record["result"])
            except Exception:
                return ActionResult("uncertain", error="wechat_send_journal_invalid")
        return ActionResult("uncertain", error="wechat_send_unconfirmed")

    def _ticket_result(self, permit):
        saved = self.store.state(self._ticket_key(permit.ticket))
        if saved is None:
            return None
        if saved.get("permit") != asdict(permit):
            return ActionResult("uncertain", error="wechat_ticket_conflict")
        if saved.get("result") is not None:
            return ActionResult(**saved["result"])
        action = self.store.state(saved["action_key"])
        return self._historical(action, permit.request_digest)

    async def prepare(self, request):
        self._check(request)
        require(len(self._pending) < 100, "wechat_queue_full")
        previous = self.store.state(self._action_key(request))
        if previous is not None:
            require(previous.get("request_digest") == request.fingerprint, "wechat_action_conflict")
            proof = None  # A prior intent/result can only be reported, never submitted again.
        else:
            try:
                proof = await asyncio.wait_for(self.driver.prepare(request), self.timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise AssistantError("wechat_driver_prepare_failed", "发送准备未完成") from None
            self._check(request)
            self._proof(request, proof)
        ticket = uuid.uuid4().hex
        self._pending[ticket] = (request, proof)
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
            }, db)
            self.store.set_state(self._ticket_key(permit.ticket), {
                "permit": asdict(permit), "action_key": self._action_key(request), "result": None,
            }, db)
        return None

    def _finish(self, request, permit, result, *, claimed):
        try:
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if claimed:
                    record = self.store.state(self._action_key(request), db=db)
                    require(record is not None and record["request_digest"] == request.fingerprint,
                            "wechat_send_journal_invalid")
                    self.store.set_state(self._action_key(request),
                                         {**record, "status": "finished", "result": asdict(result)}, db)
                self.store.set_state(self._ticket_key(permit.ticket), {
                    "permit": asdict(permit), "action_key": self._action_key(request), "result": asdict(result),
                }, db)
            return result
        except Exception:
            uncertain = claimed or result.status != "cancelled"
            return ActionResult("uncertain" if uncertain else "cancelled", error="wechat_send_journal_failed")

    @staticmethod
    def _outcome(value):
        if (not isinstance(value, DriverOutcome) or value.status not in {"confirmed", "rejected", "uncertain"}
                or type(value.client_recorded) is not bool):
            return DriverOutcome("uncertain")
        if value.status == "confirmed" and not value.client_recorded:
            return DriverOutcome("uncertain")
        if value.status == "rejected" and value.client_recorded:
            return DriverOutcome("uncertain")
        return value

    async def commit(self, permit, *, guard):
        if not isinstance(permit, SendPermit):
            return ActionResult("cancelled", error="wechat_permit_invalid")
        return await self._commit(permit, guard=guard, prepared=False)

    async def commit_prepared(self, ticket, *, guard):
        """Trusted in-process path: authorize only at the synchronous boundary.

        A prepared ticket binds an existing local request; it is not a permit.
        All guards and the durable claim still precede a new two-second permit.
        Externally supplied permits use commit and can never be renewed here.
        """
        if not isinstance(ticket, PreparedSend):
            return ActionResult("cancelled", error="wechat_prepared_mismatch")
        return await self._commit(ticket, guard=guard, prepared=True)

    async def _commit(self, permit, *, guard, prepared):
        async with self._commit_lock:
            try:
                old = self._ticket_result(permit)
            except Exception:
                return ActionResult("uncertain", error="wechat_send_journal_failed")
            if old is not None:
                return old
            pending = self._pending.pop(permit.ticket, None)
            if pending is None:
                return ActionResult("uncertain", error="wechat_ticket_unknown")
            request, proof = pending
            claimed = submitted = False
            try:
                if prepared:
                    require(permit.request_digest == request.fingerprint, "wechat_prepared_mismatch")
                else:
                    self.permits.verify(permit, request)
                previous = self.store.state(self._action_key(request))
                if previous is not None:
                    return self._finish(request, permit, self._historical(previous, request.fingerprint), claimed=False)
                self._check(request)
                guard()
                self._inspect(request, proof)
                if not prepared:
                    self.permits.verify(permit, request)
                old = self._claim(request, proof, permit)
                if old is not None:
                    return self._finish(request, permit, old, claimed=False)
                claimed = True
                # After all queueing and the durable intent: recheck permissions,
                # live target and expiry, then one synchronous native submission.
                guard()
                self._check(request)
                self._inspect(request, proof)
                # Inspection itself can observe a concurrent stop/revocation.
                # The trusted driver must also bind submit_once to this exact
                # native target, never to whichever window currently has focus.
                submission_guard = getattr(self.session, "submission_guard", None)
                with submission_guard(request) if submission_guard else nullcontext():
                    guard()
                    self._check(request)
                    authorization = self.permits.issue(permit, request) if prepared else permit
                    self.permits.verify(authorization, request)
                    submitted = True
                    outcome = self._outcome(_sync(self.driver.submit_once(request, proof)))
                confirm = getattr(self.driver, "confirm", None)
                if outcome.status == "uncertain" and callable(confirm):
                    checked = self._outcome(await asyncio.wait_for(confirm(request, proof, outcome), self.timeout))
                    # Absence of an echo never proves that a possibly submitted
                    # message was rejected or gives permission to send again.
                    outcome = checked if checked.status == "confirmed" else DriverOutcome("uncertain")
                if outcome.status == "confirmed":
                    result = ActionResult("succeeded", {"client_recorded": True, "action_id": request.action_id})
                elif outcome.status == "rejected":
                    result = ActionResult("failed", error="wechat_provider_rejected")
                else:
                    result = ActionResult("uncertain", error="wechat_send_unconfirmed")
            except asyncio.CancelledError:
                result = ActionResult("uncertain" if submitted else "cancelled", error="wechat_task_cancelled")
            except AssistantError as exc:
                result = ActionResult("uncertain" if submitted else "cancelled",
                                      error="wechat_send_unconfirmed" if submitted else exc.code)
            except Exception:
                result = ActionResult("uncertain" if submitted else "cancelled",
                                      error="wechat_send_unconfirmed" if submitted else "wechat_send_preflight_failed")
            return self._finish(request, permit, result, claimed=claimed)

    async def discard(self, ticket):
        pending = self._pending.pop(ticket, None)
        cleanup = getattr(self.driver, "discard", None)
        if pending is not None and pending[1] is not None and callable(cleanup):
            try:
                value = cleanup(pending[1])
                if inspect.isawaitable(value):
                    await asyncio.wait_for(value, self.timeout)
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
        connection = self.receiver.connection()
        return replace(connection, ready=False) if self._closed else connection

    async def acknowledge(self, cursor):
        return await self.receiver.acknowledge(cursor)

    async def poll(self, cursor, *, limit=100):
        return await self.receiver.poll(cursor, limit=limit)

    async def close(self):
        self._closed = True
        self._pending.clear()
        try:
            close = getattr(self.driver, "close", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await asyncio.wait_for(value, self.timeout)
        finally:
            await self.receiver.close()
