"""Lazy QQ adapter: bounded encrypted queue, explicit run ownership, no UI control."""
import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from companion_core import delivery, events
from companion_core.contracts import ActionResult, Capability, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, ReplyTarget, require
from companion_core.reply_policy import Participation
from companion_core.schema import obj

from . import config
from .normalization import normalize
from .transport import OneBot


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class QQModule:
    platform = "qq"
    interactive = True
    poll_interval_seconds = 1
    manages_action_quota = True

    def __init__(self, store, project, *, options=None, factory=None):
        self.store, self.project = store, Path(project)
        if options is not None:
            self.options = config.validate(options)
        else:
            try:
                self.options = config.load(project)
            except Exception:
                self.options = None  # Bad optional QQ configuration cannot stop other modules.
        self.account = self.options["account"] if self.options else "unconfigured"
        self.factory = factory or (lambda: OneBot(self.options, config.token(project, self.account)))
        self.core = self.provider = self.socket = self.reader = None
        self.ready = self.tainted = False
        self.code, self.failures, self.retry_at = "", 0, 0
        self.queue_key = "qq-inbox:" + self.account
        self.enrollment_key = "qq-enrollment:" + self.account
        self.participation = Participation(store)
        self.send_lock = asyncio.Lock()
        from .lifecycle import ManagedBridge

        self.bridge = ManagedBridge(store, self.options) if self.options else None

    def bind_runtime(self, core):
        require(core.store is self.store, "qq_store_mismatch")
        self.core = core
        self.participation = Participation(self.store, clock=core.clock)

    def capabilities(self):
        return [Capability("message.reply", "向当前已授权QQ会话发送纯文本", obj({
            "text": {"type": "string", "minLength": 1, "maxLength": 1000}})),
            Capability("conversation.end", "仅群聊：话题已经结束或转向无关讨论时，停止本轮参与，等待再次@或引用。不会发消息。", obj({}))]

    def reply_targets(self):
        if self.options is None:
            return ()
        saved = self.store.state(self.enrollment_key)
        if not saved or saved.get("intent") != digest(self.options):
            return ()
        return tuple(ReplyTarget(ReplyScope("qq", self.account, kind, target),
            saved["identity"].get("names", {}).get(kind, {}).get(target, ""),
            saved["identity"]["account_label"] or self.account,
            "私聊直接回应；群聊须@或引用唤起，随后限额参与")
            for kind, field in (("private", "private_targets"), ("group", "groups")) for target in self.options[field])

    def probe(self):
        """Explicit read-only setup. Saves only the configured, verified targets."""
        require(self.options is not None, "qq_configuration")
        provider = self.factory()
        try:
            identity = provider.probe()
            require(identity["account"] == self.account, "qq_account_mismatch")
            value = {"intent": digest(self.options), "identity": identity, "at": datetime.now(timezone.utc).isoformat()}
            with self.store.lock:
                require(self.store.ledger_encode("qq-preflight", {}).startswith("enc1:"), "qq_store_unencrypted")
                self.store.set_state(self.enrollment_key, value)
            return {"verified": True, "private_count": len(self.options["private_targets"]),
                    "group_count": len(self.options["groups"]), "messages_sent": 0, "role_started": False}
        finally:
            provider.close()

    def validate_start(self):
        require(self.options is not None, "qq_configuration")
        require(self.provider is None and self.reader is None, "qq_stopping")
        require(bool(self.reply_targets()), "qq_enrollment_required")
        self.provider = self.factory()
        self.tainted = False
        self.code, self.failures, self.retry_at = "", 0, 0

    def validate_run(self, run):
        require(self.provider is not None, "qq_not_started")
        require(not self.tainted, "qq_send_unconfirmed")

    def scope(self, event):
        return ReplyScope("qq", self.account, event.visibility, event.metadata["qq"]["target_id"])

    def sent_key(self, kind, target, identifier):
        return stable_key("qq-sent", self.account, kind, target, identifier)

    def accept(self, raw):
        """Called only by the authenticated transport, never a model tool."""
        event = normalize(raw, self.options, sent_reference=lambda kind, target, identifier:
                          self.store.state(self.sent_key(kind, target, identifier)) is not None)
        if event is None:
            return
        stamp = datetime.fromisoformat(event.occurred_at).timestamp()
        now = self.participation.now()
        if not now - 300 <= stamp <= now + 30:
            return
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            key, fingerprint = "qq-seen:" + event.id, digest(event.as_dict())
            previous = self.store.state(key, None, db)
            if previous is not None:
                require(previous == fingerprint, "qq_event_conflict")
                return
            self.store.set_state(key, fingerprint, db)
            if ReplyAuthorizations(self.store).version(self.scope(event), db=db) is None:
                return
            receipt = events.ingest(self.store, event)
            require(not receipt["changed"], "qq_event_conflict")
            queue = self.store.state(self.queue_key, [], db)
            require(len(queue) < 512, "qq_inbox_backpressure")
            self.store.set_state(self.queue_key, queue + [event.id], db)

    async def _read(self, socket):
        try:
            async for raw in socket:
                self.accept(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except AssistantError as exc:
            self.code = exc.code
        except Exception:
            self.code = "qq_provider_unavailable"
        finally:
            self.ready = False
            self.failures += 1
            self.retry_at = time.monotonic() + min(60, 2 ** min(self.failures, 6))

    async def _connect(self):
        if self.ready or time.monotonic() < self.retry_at:
            return
        if self.reader is not None:
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.socket is not None:
            await self.socket.close()
            self.socket = None
        try:
            identity = (await self.bridge.ensure(self.provider, self.core.guard) if self.options.get("launch") else
                        await asyncio.to_thread(self.provider.probe))
            require(identity["account"] == self.account, "qq_account_mismatch")
            self.store.set_state(self.enrollment_key, {"intent": digest(self.options), "identity": identity,
                                                       "at": datetime.now(timezone.utc).isoformat()})
            self.socket = await self.provider.connect()
            # Recheck both sides of the connection before accepting any event.
            await asyncio.to_thread(self.provider.identity)
            await asyncio.to_thread(self.core.guard)
            self.ready, self.code, self.failures = True, "", 0
            if self.bridge:
                self.bridge.state = "connected"
            self.reader = asyncio.create_task(self._read(self.socket))
        except AssistantError as exc:
            self.code = exc.code
            self.failures += 1
            self.retry_at = time.monotonic() + min(60, 2 ** min(self.failures, 6))
            if self.options.get("launch") or exc.code in {"qq_account_mismatch", "qq_target_unverified"}:
                if self.bridge:
                    await asyncio.to_thread(self.bridge.stop)
                raise

    async def runtime_poll(self):
        run = await asyncio.to_thread(self.core.guard)
        if self.code not in {"", "qq_provider_unavailable", "qq_login_required"}:
            raise AssistantError(self.code, "QQ连接器需要检查，其他平台保持原状态")
        await self._connect()
        if not self.ready or run["read_only"]:
            return []
        result, selected_threads = [], set()
        batch_size = self.store.read("companion")["data"].get("inbox_batch_size", 3)
        with self.store.lock, self.store.db() as db:
            queue = self.store.state(self.queue_key, [], db)
            retained = []
            for event_id in queue:
                if len(result) >= batch_size:
                    retained.append(event_id)
                    continue
                try:
                    saved = events.get(self.store, event_id)
                except AssistantError as exc:
                    if exc.code == "unknown_evidence":
                        continue
                    raise
                if self.store.state("core-processed:" + event_id, None, db) == saved["version"]:
                    continue
                event = Event.from_dict(saved["event"])
                if event.thread in selected_threads:
                    retained.append(event_id)
                    continue
                if (datetime.fromisoformat(event.occurred_at).timestamp() < self.participation.now() - 300):
                    continue
                key = "qq-admission:" + event_id
                admission = self.store.state(key, None, db)
                if admission is None:
                    if datetime.fromisoformat(event.occurred_at).timestamp() < datetime.fromisoformat(run["started_at"]).timestamp() - 3:
                        continue
                    ticket = self.participation.admit(self.scope(event), event.id, event.metadata["qq"]["trigger"], run["generation"])
                    if ticket is None:
                        continue
                    admission = {"ticket": ticket, "version": saved["version"]}
                    self.store.set_state(key, admission, db)
                elif event.visibility == "private" and admission["ticket"]["generation"] != run["generation"]:
                    # Only already admitted, fresh private input can resume;
                    # current policy/permission and the action journal still gate it.
                    admission["ticket"]["generation"] = run["generation"]
                    self.store.set_state(key, admission, db)
                if not self.participation.eligible(admission["ticket"], run["generation"]):
                    continue
                retained.append(event_id)
                selected_threads.add(event.thread)
                if delivery.eligible(self.core, event, saved["version"], run):
                    result.append(event)
            self.store.set_state(self.queue_key, retained, db)
        return result

    async def receive(self, cursor):
        return await self.runtime_poll(), {}

    def validate_event(self, event):
        require(event.platform == "qq" and event.account == self.account, "qq_event_scope")
        require(self.ready, "qq_provider_unavailable")
        run = self.core.guard()
        saved = events.get(self.store, event.id)
        require(digest(saved["event"]) == digest(event.as_dict()), "qq_event_conflict")
        admission = self.store.state("qq-admission:" + event.id)
        require(admission is not None and admission["version"] == saved["version"], "qq_event_untrusted")
        self.participation.check(admission["ticket"], run["generation"])
        return admission["ticket"], run["generation"]

    @asynccontextmanager
    async def decision_material(self, event, *, guard):
        await asyncio.to_thread(guard)
        ticket, generation = self.validate_event(event)
        yield {"context": self.participation.context(ticket, generation), "images": ()}

    async def read(self, reference):
        raise AssistantError("qq_read_unsupported", "QQ模块不提供任意聊天读取")

    def _submit(self, event, capability, arguments, guard):
        submitted, action_id = False, None
        try:
            with self.store.lock:
                guard()
                ticket, generation = self.validate_event(event)
                key = stable_key("dispatch", "qq", self.account, event.id, capability)
                with self.store.db() as db:
                    row = db.execute("SELECT * FROM core_actions WHERE dedupe=?", (key,)).fetchone()
                require(row is not None and row["status"] == "dispatching" and row["generation"] == generation, "qq_action_missing")
                body = self.store.ledger_decode("core_actions.body", row["body"])
                require(digest(body["event"]) == digest(event.as_dict()) and body["arguments"] == arguments, "qq_action_mismatch")
                if capability == "conversation.end":
                    self.participation.end(ticket, generation)
                    return ActionResult("succeeded", {"participation_ended": True})
                require(capability == "message.reply", "qq_action_unsupported")
                text = arguments.get("text")
                require(isinstance(text, str) and 0 < len(text) <= 1000 and text.strip(), "qq_text_invalid")
                self.provider.identity()
                guard()
                self.participation.check(ticket, generation)
                action_id = row["id"]
                self.participation.reserve(ticket, generation, action_id)
                submitted = True
                identifier = self.provider.send(self.scope(event), text)
                self.store.set_state(self.sent_key(event.visibility, self.scope(event).target_id, identifier), {"action": action_id})
                self.participation.settle(action_id, "succeeded")
                return ActionResult("succeeded", {"message_id": identifier, "provider_accepted": True})
        except BaseException as exc:
            outcome = "uncertain" if submitted else "cancelled"
            if submitted:
                self.tainted = True
            if action_id:
                self.participation.settle(action_id, outcome)
            if not isinstance(exc, Exception):
                raise
            return ActionResult(outcome, error="qq_send_unconfirmed" if submitted else getattr(exc, "code", "qq_preflight_failed"))

    async def execute(self, capability, arguments, *, guard):
        async with self.send_lock:
            event = Event.from_dict(arguments["_event"])
            values = {k: v for k, v in arguments.items() if not k.startswith("_")}
            # Cancellation cannot abandon a background HTTP submit and falsely
            # report it unsent. Its bounded outcome must finish journaling.
            task = asyncio.create_task(asyncio.to_thread(self._submit, event, capability, values, guard))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def runtime_stopped(self, generation):
        self.ready = False

    async def idle(self):
        await self.close()

    async def close(self):
        # Native submits are shielded and journalled before the bridge can exit.
        async with self.send_lock:
            await self._close()

    async def _close(self):
        self.ready = False
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.socket:
            await self.socket.close()
            self.socket = None
        if self.provider:
            self.provider.close()
            self.provider = None
        if self.bridge:
            await asyncio.to_thread(self.bridge.stop)

    def runtime_status(self):
        return {"state": "connected" if self.ready else (
                    self.bridge.state if self.bridge and self.options.get("launch") else
                    "retrying" if self.failures or self.code else "new") if self.provider else "paused",
                "code": self.code, "pending": len(self.store.state(self.queue_key, [])),
                "uncertain": self.tainted, "automatic_start": False}


def configured_module(store, project):
    return QQModule(store, project) if (Path(project) / ".dev/qq/module.json").exists() else None
