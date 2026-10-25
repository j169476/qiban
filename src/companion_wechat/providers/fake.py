"""Synthetic gateway only. This module cannot open a client or send a platform message."""
import asyncio
import uuid

from companion_core.contracts import ActionResult
from companion_core.errors import AssistantError

from ..contracts import Batch, Connection, PreparedSend, require


class FakeGateway:
    simulation = True

    def __init__(self, account_identity, permits, *, epoch="synthetic-epoch", capacity=1000, inbox_store=None):
        self.session = Connection(account_identity, epoch)
        from ..durable_inbox import DurableInbox

        self.durable = (DurableInbox(inbox_store, account_identity, epoch, capacity=min(capacity, 500))
                        if inbox_store is not None else None)
        self.permits, self.capacity = permits, capacity
        self.stream = uuid.uuid4().hex
        self.acknowledged = self.offered = self.sequence = 0
        self.inbox, self.sent = [], []
        self.targets, self.pending, self.completed, self.ticket_results = {}, {}, {}, {}
        self.paused_targets, self.revoked = set(), set()
        self.after_prepare = lambda: None
        self.before_commit = lambda: None
        self.after_submit = lambda: None
        self.outcome = "confirmed"
        self._commit_lock = asyncio.Lock()

    def connection(self):
        return self.session

    def bind_target(self, binding):
        self.targets[binding.target_ref] = (binding.key, binding.revision)

    def publish(self, observation):
        if self.durable is not None:
            return self.durable.publish(observation)
        require(len(self.inbox) < self.capacity, "wechat_inbox_full")
        self.sequence += 1
        self.inbox.append((self.sequence, observation))

    def _offset(self, cursor):
        if not cursor or set(cursor) <= {"since"}:
            require(self.acknowledged == 0, "wechat_cursor_regressed")
            return 0
        require(set(cursor) == {"wechat_stream", "offset"}
                and cursor["wechat_stream"] == self.stream, "wechat_cursor_stream")
        value = cursor["offset"]
        require(type(value) is int and self.acknowledged <= value <= self.offered,
                "wechat_cursor_invalid")
        return value

    async def acknowledge(self, cursor):
        if self.durable is not None:
            return await self.durable.acknowledge(cursor)
        offset = self._offset(cursor)
        self.inbox = [(seq, observation) for seq, observation in self.inbox if seq > offset]
        self.acknowledged = offset

    async def poll(self, cursor, *, limit):
        if self.durable is not None:
            return await self.durable.poll(cursor, limit=limit)
        offset = self._offset(cursor)
        selected = [(seq, item) for seq, item in self.inbox if seq > offset][:limit]
        end = selected[-1][0] if selected else offset
        self.offered = max(self.offered, end)
        return Batch(tuple(item for _, item in selected), {"wechat_stream": self.stream, "offset": end})

    def _check(self, request):
        require(self.session.ready, "wechat_disconnected")
        require((self.session.account_identity, self.session.epoch)
                == (request.account_identity, request.epoch), "wechat_stale_connection")
        require(self.targets.get(request.target_ref) == (request.conversation, request.revision),
                "wechat_binding_changed")
        require(request.target_ref not in self.paused_targets, "wechat_manual_takeover")
        require(request.generation not in self.revoked, "core_before_send_cancelled")

    async def prepare(self, request):
        self._check(request)
        require(len(self.pending) < 100, "wechat_queue_full")
        ticket = uuid.uuid4().hex
        self.pending[ticket] = request
        try:
            self.after_prepare()
        except BaseException:
            self.pending.pop(ticket, None)
            raise
        return PreparedSend(ticket, request.fingerprint)

    async def commit(self, permit, *, guard):
        async with self._commit_lock:
            self.before_commit()
            previous = self.ticket_results.get(permit.ticket)
            if previous:
                return (previous[1] if previous[0] == permit
                        else ActionResult("uncertain", error="wechat_ticket_conflict"))
            request = self.pending.pop(permit.ticket, None)
            if request is None:
                return ActionResult("cancelled", error="wechat_ticket_consumed")
            try:
                self.permits.verify(permit, request)
                self._check(request)
                # Queueing and target preparation have finished. There is no
                # await between this check and the synthetic submission below.
                guard()
                self.permits.verify(permit, request)
                self._check(request)
            except AssistantError as exc:
                return ActionResult("cancelled", error=exc.code)
            if request.action_id in self.completed:
                fingerprint, previous = self.completed[request.action_id]
                result = (previous if fingerprint == request.fingerprint
                          else ActionResult("uncertain", error="wechat_action_conflict"))
                self.ticket_results[permit.ticket] = (permit, result)
                return result
            if self.outcome == "rejected":
                result = ActionResult("failed", error="wechat_provider_rejected")
            else:
                self.sent.append(request)
                if self.outcome in ("click_only", "timeout_after"):
                    result = ActionResult("uncertain", error="wechat_send_unconfirmed")
                else:
                    result = ActionResult("succeeded", {"simulated": True, "client_observed": True,
                                                        "action_id": request.action_id})
            self.completed[request.action_id] = (request.fingerprint, result)
            self.ticket_results[permit.ticket] = (permit, result)
            self.after_submit()
            if self.outcome == "timeout_after":
                raise TimeoutError("synthetic transport timeout")
            return result

    async def discard(self, ticket):
        self.pending.pop(ticket, None)

    def revoke_generation(self, generation):
        self.revoked.add(generation)
