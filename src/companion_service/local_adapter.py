"""Private local messages use a server-authenticated actor, never a posted UID."""
from __future__ import annotations

import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from companion_core import events
from companion_core.access import AccessService
from companion_core.contracts import ActionResult, Capability, Event
from companion_core.errors import AssistantError
from companion_core.input_images import InputImages, identifiers
from companion_core.schema import obj


class LocalAdapter:
    platform = "local"
    interactive = True
    action_budget_field = "daily_local_replies"
    reply_quota_preflight = True

    def __init__(self, store, sessions):
        self.store, self.sessions = store, sessions
        self.account = store.read("manifest")["data"]["project_id"]
        self.core = None
        self.images = InputImages(store, sessions.authority)

    def bind_runtime(self, core):
        self.core = core

    def capabilities(self):
        return [Capability("message.reply", "在当前已验证的本机私密会话回复", obj({
            "text": {"type": "string", "minLength": 1, "maxLength": 12000}}))]

    async def receive(self, cursor):
        # POST commits to the shared inbox before acknowledging reception.
        return [], cursor

    async def read(self, reference):
        raise AssistantError("unsupported", "本机入口不支持任意资料读取")

    def post(self, token, text, nonce, images=None):
        if (not isinstance(text, str) or not text.strip() or len(text) > 12000
                or not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce)):
            raise AssistantError("invalid_message", "消息为空、过长或提交标识无效")
        run = self.core.guard()
        if run["read_only"]:
            raise AssistantError("read_only", "本次运行只读，请先停止再显式启动对话模式")
        request = self.sessions.request(token, "role")
        actor = self.sessions.actor(request)
        event = Event(self.platform, self.account, nonce, actor, actor.key,
                      datetime.now(timezone.utc).isoformat(), text.strip(), "private",
                      metadata={"session": request.session_id, "generation": run["generation"]})
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = self.request(token)
            event = replace(event, attachments=self.images.bind(request, images or [], event))
            with self.store.db() as db:
                previous = db.execute("SELECT body FROM core_events WHERE id=?", (event.id,)).fetchone()
                pending = db.execute("SELECT count(*) FROM core_events e LEFT JOIN core_event_receipts r ON e.id=r.event_id "
                                     "WHERE e.platform=? AND e.account=? AND e.deleted=0 AND r.event_id IS NULL",
                                     (self.platform, self.account)).fetchone()[0]
            if previous:
                old = Event.from_dict(self.store.ledger_decode("core_events.body", previous[0]))
                if old.actor != actor or old.text != text.strip() or old.attachments != event.attachments:
                    raise AssistantError("message_conflict", "提交标识已被使用")
                return {"id": event.id, "duplicate": True}
            if pending >= 20:
                raise AssistantError("inbox_full", "待处理消息过多，请等待当前回复")
            result = events.ingest(self.store, event)
            # Attachments are input sources, so revocation invalidates user turns and all derived outputs.
            if event.attachments:
                repository = self.sessions.repository
                policy = repository.policy("event/" + event.id)
                pins = tuple(("input_images/" + key, repository.policy("input_images/" + key).version) for key in identifiers(event))
                repository.set_policy(replace(policy, sources=tuple(key for key, _ in pins), source_versions=pins),
                                      policy.version, request.actor, db=db)
            return result

    def validate_event(self, event):
        owner = self.store.state("web-owner", {})
        from companion_core.contracts import ActorRef

        actor = ActorRef(**owner["actor"]) if owner else None
        run = self.core.guard()
        if (not actor or actor != event.actor or event.visibility != "private" or event.conversation != actor.key
                or not self.sessions.repository.session_active(event.metadata.get("session", ""))
                or event.metadata.get("generation") != run["generation"]):
            raise AssistantError("session_expired", "本机消息的会话或运行已结束，需要重新提交")
        if self.store.state("web-message:" + event.id, {}).get("reason") == "user_cancelled":
            raise AssistantError("session_expired", "该消息已由用户取消")

    def request(self, token):
        request = self.sessions.request(token, "role")
        actor = self.sessions.actor(request)
        thread = Event(self.platform, self.account, "web", actor, actor.key, "", "").thread
        return self.sessions.request(token, "role", conversation=thread)

    @asynccontextmanager
    async def decision_material(self, event, *, guard):
        with tempfile.TemporaryDirectory(prefix="input-image-", dir=self.store.runtime) as folder:
            request = self.sessions._recover(event.metadata["session"], "role", conversation=event.thread)
            files = []
            for key in identifiers(event):
                guard()
                target = Path(folder) / (key + ".png")
                target.write_bytes(self.images.read(request, key))
                files.append(target)
            guard()
            yield {"context": {"images_read": len(files), "input": "authenticated_private_upload"}, "images": files}

    async def execute(self, capability, arguments, *, guard):
        guard()
        # The core records the response transactionally; no external delivery.
        return ActionResult("succeeded", {"local": True})

    def conversation(self):
        from companion_core.contracts import ActorRef

        owner = self.store.state("web-owner", {})
        if not owner:
            return None
        actor = ActorRef(**owner["actor"])
        return Event(self.platform, self.account, "maintenance", actor, actor.key, "", "").thread

    def maintenance_request(self):
        conversation = self.conversation()
        if conversation is None:
            return None
        with self.store.db() as db:
            row = db.execute("SELECT id FROM governance_sessions WHERE purpose='role' AND revoked=0 "
                             "AND expires_at>? AND epoch=? ORDER BY expires_at DESC LIMIT 1",
                             (self.sessions.repository.clock(), self.sessions.repository.epoch())).fetchone()
        if row is None:
            return None
        try:
            return self.sessions._recover(row["id"], "role", conversation=conversation)
        except AssistantError as exc:
            if exc.code == "login_required":
                return None
            raise

    def history(self, token):
        return self.page(token, limit=100)["messages"]

    def page(self, token, **options):
        from companion_core.conversation_query import page

        request = self.request(token)
        return page(self.store, self.sessions.authority, request, self.account, self.platform, **options)

    def queue(self, token):
        request = self.request(token)
        access = AccessService(self.sessions.repository, self.sessions.authority)
        with self.store.db() as db:
            rows = db.execute("SELECT e.id,e.deleted,e.body,r.version AS processed FROM core_events e "
                "LEFT JOIN core_event_receipts r ON r.event_id=e.id WHERE e.platform='local' AND e.account=? "
                "AND e.actor=? AND e.conversation=? ORDER BY e.occurred DESC,e.id DESC LIMIT 20",
                (self.account, request.actor, request.conversation)).fetchall()
        output = []
        for row in rows:
            state = self.store.state("web-message:" + row["id"], {})
            status = "cancelled" if row["deleted"] else state.get("status", "completed" if row["processed"] else "queued")
            item = {"id": row["id"], "status": status, "reason": state.get("reason", ""), "due_at": state.get("due_at")}
            if not row["deleted"] and access.can_read(request, "event/" + row["id"]):
                lease = access.lease(request, ["event/" + row["id"]])
                item["text"] = events.get(self.store, row["id"])["event"]["text"][:160]
                access.revalidate(lease)
            output.append(item)
        self.sessions.authority.require(request)
        return output

    def cancel(self, token, event_id):
        request = self.request(token)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT actor,conversation,version FROM core_events WHERE id=? AND platform='local' AND account=?",
                             (event_id, self.account)).fetchone()
            if not row or row["actor"] != request.actor or row["conversation"] != request.conversation:
                raise AssistantError("access_denied", "消息不属于当前会话")
            # Action dedupe is a stable, scope-bound key; inspect only actions for this event.
            from companion_core.contracts import stable_key

            key = stable_key("dispatch", self.platform, self.account, event_id, "message.reply")
            action = db.execute("SELECT status FROM core_actions WHERE dedupe=?", (key,)).fetchone()
            if action and action[0] in {"dispatching", "succeeded", "uncertain"}:
                raise AssistantError("message_sent", "回复已经发送或正在确认，不能取消")
            if self.store.state("core-processed:" + event_id) == row["version"]:
                raise AssistantError("message_sent", "消息已经处理完成")
            events.forget(self.store, event_id)
            self.store.set_state("core-processed:" + event_id, row["version"], db)
            self.store.set_state("web-message:" + event_id, {"status": "cancelled", "reason": "user_cancelled"}, db)
        return {"cancelled": True}
