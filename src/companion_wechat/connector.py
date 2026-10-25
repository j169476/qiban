"""PlatformPort; live replies require a separate verified sender implementation."""
import asyncio

from companion_core.contracts import ActionResult, Capability, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.schema import obj

from .contracts import SendRequest, digest, require
from .normalization import Normalizer


class WeChatConnector:
    platform = "wechat"
    interactive = True

    def __init__(self, bindings, gateway, permits, *, allow_simulated_send=False, timeout=5.0,
                 read_only_collection=False, include_untriggered_group=False, sender=None, verified_sender_ids=False):
        require(0 < timeout <= 30)
        self.bindings, self.gateway, self.permits = bindings, gateway, permits
        self.store, self.account = bindings.store, bindings.account
        self.read_only_collection = read_only_collection
        self.normalizer = Normalizer(bindings, include_untriggered_group=(
            read_only_collection or include_untriggered_group), verified_sender_ids=verified_sender_ids)
        self.sender = sender if sender is not None else gateway
        if sender is not None:
            # Only the dedicated trusted boundary can enable live submission;
            # changing a provider's simulation flag is never sufficient.
            from .live_sender import LiveSenderGateway
            from .supervised_sender import SupervisedSenderGateway

            require(isinstance(sender, (LiveSenderGateway, SupervisedSenderGateway)) and sender.store is self.store
                    and sender.receiver is gateway and sender.permits is permits, "wechat_sender_invalid")
        self.allow_simulated_send, self.timeout = allow_simulated_send, timeout
        self.core = None
        self._receive_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    def bind_runtime(self, core):
        require(core.store is self.store, "wechat_store_mismatch")
        self.core = core

    def capabilities(self):
        if self.read_only_collection:
            return []
        return [Capability("message.reply", "向当前已绑定微信会话回复文本", obj({
            "text": {"type": "string", "minLength": 1, "maxLength": 12000}}))]

    async def read(self, reference):
        raise AssistantError("wechat_read_unsupported", "微信模块不提供任意账号或聊天资料读取")

    def _connection(self):
        connection = self.gateway.connection()
        require(connection.ready, "wechat_disconnected")
        require(connection.account_identity == self.bindings.identity, "wechat_account_mismatch")
        return connection

    async def receive(self, cursor):
        require(isinstance(cursor, dict))
        async with self._receive_lock:
            try:
                connection = self._connection()
                # Only the cursor returned by the core on its NEXT poll confirms
                # persistence. This batch remains unacknowledged on every failure.
                await asyncio.wait_for(self.gateway.acknowledge(cursor), self.timeout)
                batch = await asyncio.wait_for(self.gateway.poll(cursor, limit=100), self.timeout)
                require(self._connection() == connection, "wechat_stale_connection")
                require(len(batch.observations) <= 100 and isinstance(batch.cursor, dict))
                return self.normalizer.batch(batch.observations, connection), batch.cursor
            except AssistantError:
                raise
            except Exception:
                raise AssistantError("wechat_receive_failed", "微信收件失败，本批尚未确认") from None

    def validate_event(self, event):
        require(event.platform == self.platform and event.account == self.account, "wechat_event_scope")
        reservation = self.store.state(stable_key("wechat-observation-v1", self.account, event.native_id))
        require(reservation is not None and digest(reservation["event"]) == digest(event.as_dict()),
                "wechat_event_untrusted")
        with self.store.db() as db:
            source = db.execute("SELECT body,deleted FROM core_events WHERE id=?", (event.id,)).fetchone()
            require(source is not None and not source["deleted"]
                    and digest(self.store.ledger_decode("core_events.body", source["body"])) == digest(event.as_dict()),
                    "wechat_event_revoked")
        connection = self._connection()
        binding = self.bindings.get(event.conversation)
        meta = event.metadata.get("wechat", {})
        require(binding.enabled, "wechat_target_disabled")
        require(event.visibility == binding.kind
                and meta.get("target_ref") == binding.target_ref
                and meta.get("binding_revision") == binding.revision, "wechat_binding_changed")
        require(meta.get("epoch") == connection.epoch, "wechat_stale_connection")
        require(binding.kind != "private" or (event.actor.user_id == binding.peer_ref
                                             and event.actor.scope == self.account), "wechat_sender_mismatch")
        return binding, connection

    def _request(self, event, text):
        require(self.core is not None, "wechat_runtime_missing")
        run = self.core.guard()
        require(not run["read_only"], "wechat_read_only")
        binding, connection = self.validate_event(event)
        dedupe = stable_key("dispatch", self.platform, self.account, event.id, "message.reply")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM core_actions WHERE dedupe=?", (dedupe,)).fetchone()
            require(row is not None and row["status"] == "dispatching", "wechat_action_missing")
            body = self.store.ledger_decode("core_actions.body", row["body"])
            require(row["generation"] == run["generation"] and digest(body["event"]) == digest(event.as_dict())
                    and body["arguments"] == {"text": text}, "wechat_action_mismatch")
            # Read the real attempt ID from the core ledger, including retries.
            return SendRequest(row["id"], run["generation"], connection.account_identity, connection.epoch,
                               binding.key, binding.revision, binding.target_ref, text)

    async def execute(self, capability, arguments, *, guard):
        prepared = None
        submitted = False
        async with self._send_lock:
            try:
                require(not self.read_only_collection, "wechat_read_only")
                if self.sender is self.gateway:
                    require(self.allow_simulated_send and self.gateway.simulation, "wechat_live_send_unavailable")
                else:
                    from .live_sender import LiveSenderGateway
                    from .supervised_sender import SupervisedSenderGateway

                    require(isinstance(self.sender, (LiveSenderGateway, SupervisedSenderGateway)),
                            "wechat_live_send_unavailable")
                require(capability == "message.reply" and isinstance(arguments, dict), "wechat_unsupported_action")
                require(set(arguments) <= {"text", "_event", "_retry_unsent", "_disclosure"})
                text = arguments.get("text")
                require(isinstance(text, str) and 0 < len(text.strip()) <= 12000 and len(text) <= 12000)
                event = Event.from_dict(arguments["_event"])
                guard()
                request = self._request(event, text)
                from .supervised_sender import SupervisedSenderGateway

                supervised = isinstance(self.sender, SupervisedSenderGateway)
                prepare_timeout = self.sender.prepare_timeout if supervised else self.timeout
                commit_timeout = self.sender.commit_timeout if supervised else self.timeout
                prepared = await asyncio.wait_for(self.sender.prepare(request), prepare_timeout)
                # Preparing a window may yield. Freeze the same action, binding,
                # account, connection and generation again immediately before send.
                guard()
                require(self._request(event, text) == request, "wechat_send_context_changed")
                permit = self.permits.issue(prepared, request)

                def final_guard():
                    guard()
                    require(self._request(event, text) == request, "wechat_send_context_changed")

                submitted = True
                result = await asyncio.wait_for(self.sender.commit(permit, guard=final_guard), commit_timeout)
                require(isinstance(result, ActionResult), "wechat_invalid_result")
                return result
            except asyncio.CancelledError:
                # Core has already committed dispatching. Leave unknown submission
                # uncertain even when the scheduler itself is cancelled.
                return ActionResult("uncertain" if submitted else "cancelled", error="wechat_task_cancelled")
            except AssistantError as exc:
                return ActionResult("uncertain" if submitted else "cancelled", error=exc.code)
            except Exception:
                return ActionResult("uncertain" if submitted else "failed", error="wechat_transport_failed")
            finally:
                if prepared is not None:
                    try:
                        await asyncio.wait_for(self.sender.discard(prepared.ticket), self.timeout)
                    except (Exception, asyncio.CancelledError):
                        pass

    def runtime_stopped(self, generation):
        self.sender.revoke_generation(generation)
