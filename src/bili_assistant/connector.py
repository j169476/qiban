"""Bilibili implementation of the core platform protocol and legacy data mapping."""
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from companion_core import events
from companion_core.contracts import ActionResult, ActorRef, Capability, Event
from companion_core.errors import AssistantError
from companion_core.identity import IdentityService
from companion_core.schema import obj


def normalize(account, event):
    from .conversations import event_thread

    private = event["kind"] == "dm_reply"
    channel, key = event_thread(event)
    thread = channel + ":" + key
    metadata = {"legacy_event_id": event["id"], "kind": event["kind"]}
    if event.get("resource_type", 1) != 1:
        metadata["resource_type"] = event["resource_type"]
    return Event("bilibili", account, event["id"], ActorRef("bilibili", str(event["uid"])), thread,
                 event["received"], event.get("text", ""), "private" if private else "public",
                 tuple(event.get("attachments", [])), metadata)


def record_events(store, account, incoming):
    return [events.ingest(store, normalize(account, item)) for item in incoming]


def _material_event(store, event, *, create=False):
    """Validate a decision's material without overwriting a corrected source event."""
    with store.lock:
        with store.db() as db:
            row = db.execute("SELECT body,version,deleted FROM core_events WHERE id=?", (event.id,)).fetchone()
        if row is None and create:
            events.ingest(store, event)
            return events.get(store, event.id)
        if row is None or row["deleted"] or store.ledger_decode("core_events.body", row["body"]) != json.loads(json.dumps(event.as_dict())):
            raise AssistantError("context_changed", "入站原文已被修改或删除，请重新读取材料")
        return {"event": store.ledger_decode("core_events.body", row["body"]), "version": row["version"]}


def identity_packet(engine, incoming, *, with_dependencies=False):
    from companion_core.public_workspace import PublicWorkspace

    if isinstance(engine.store, PublicWorkspace):
        proof = [_material_event(engine.store, normalize(engine.account, item), create=True)
                 for item in incoming if item["kind"] != "dm_reply"]
        value = {"identity_evidence": proof, "people": []}
        return (value, []) if with_dependencies else value
    account = engine.store.read("manifest")["data"]["account_uid"]
    proof, people, dependencies = [], [], []
    service = IdentityService(engine.store)
    contexts, actor_events = {}, {}
    with engine.store.lock:
        normalized = [normalize(account, item) for item in incoming if item.get("received")]
        for event in normalized:
            proof.append(_material_event(engine.store, event, create=True))
            actor_events.setdefault(event.actor.key, []).append(event.id)
        for event in normalized:
            if event.actor.key not in contexts:
                contexts[event.actor.key] = service.decision_context(
                    event.actor, input_event_ids=actor_events[event.actor.key])
                dependencies.append(contexts[event.actor.key]["dependency"])
            context = contexts[event.actor.key]
            people.append({**context["identity"], "profile": context["person_profile"]})
    packet = {"identity_evidence": proof, "identity_people": people}
    return (packet, dependencies) if with_dependencies else packet


def apply_identity(engine, incoming, proposals):
    account = engine.store.read("manifest")["data"]["account_uid"]
    with engine.store.lock:
        allowed = {_material_event(engine.store, normalize(account, item), create=True)["event"]["id"]
                   for item in incoming if item.get("received")}
        return IdentityService(engine.store).apply_model(proposals, allowed_evidence=allowed)


async def review_identities(scheduler, incoming):
    engine = scheduler.engine
    from companion_core.public_workspace import PublicWorkspace

    if isinstance(engine.store, PublicWorkspace):
        return
    account = engine.store.read("manifest")["data"]["account_uid"]
    service = IdentityService(engine.store)
    seen = set()
    for item in incoming:
        if not item.get("received") or scheduler.remaining_model_calls() < 2:
            continue
        event = normalize(account, item)
        _material_event(engine.store, event, create=True)
        if event.actor.key not in seen:
            await service.review(event.actor, scheduler.model, scheduler.alive)
            seen.add(event.actor.key)


class BilibiliConnector:
    platform = "bilibili"
    manages_action_quota = True

    def __init__(self, engine):
        self.engine = engine
        self.account = engine.store.read("manifest")["data"]["account_uid"]

    def bind_runtime(self, core):
        from .core_binding import CoreBoundEngine

        if core.store.root != self.engine.store.root:
            raise AssistantError("connector_store_mismatch", "B站连接器必须使用统一核心的同一数据空间")
        # Preserve the caller's controller; only this adapter view changes ownership.
        self.engine = CoreBoundEngine(self.engine, core, self.account)

    def validate_start(self):
        self.engine.validate_start()

    def validate_run(self, run):
        self.engine.validate_run(run)

    def runtime_stopped(self, generation, *, db=None):
        self.engine.runtime_stopped(generation, db=db)

    def workflow(self, core):
        from companion_core.governance import enabled

        if enabled(core.store):
            from .governed_workflow import definition

            return definition(self.engine, core)
        from .core_binding import CoreScheduler
        from .workflow import BilibiliWorkflow

        return BilibiliWorkflow(CoreScheduler(self.engine, core)).definition()

    def relationship_id(self, actor):
        if actor.platform != self.platform:
            raise AssistantError("connector_scope", "B站关系档案只能映射本平台主体")
        return actor.user_id

    async def read_private_task(self, task, previous, **options):
        from .private_tasks import read_window

        return await read_window(self, task, previous, **options)

    def conversation_scope(self, event):
        channel, key = event.conversation.split(":", 1)
        return {"account": self.account, "channel": "dm" if channel == "dm" else "comment", "key": key,
                "message_id": event.native_id, "sender_uid": event.actor.user_id, "reply_to": ""}

    def reply_content(self, event, capability, arguments, outcome):
        # Engine records platform receipts in the same mapped conversation, with native IDs.
        return None

    def recover_conversation(self, event):
        from .conversations import recover_platform_turns

        # Native receipts may have committed just before a crash in YAML archiving.
        # This repairs history on direct replay too, without sending or changing receipts.
        recover_platform_turns(self.engine)

    def capabilities(self):
        text = {"type": "string", "minLength": 1, "maxLength": 1000}
        video = {"type": "string", "pattern": "^BV[A-Za-z0-9]{10}$"}
        result = [Capability("message.reply", "回复本次实际收到的私信或评论", obj({"text": text}))]
        for kind in ("like", "favorite", "coin", "comment"):
            fields = {"bvid": video}
            if kind == "coin":
                fields["units"] = {"enum": [1, 2]}
            if kind == "comment":
                fields["text"] = text
            result.append(Capability("bilibili." + kind, kind, obj(fields)))
        return result

    async def receive(self, cursor):
        self.engine.role_run()
        incoming = await self.engine.inbox()
        return [normalize(self.account, item) for item in incoming], dict(cursor)

    async def read(self, reference):
        if reference.get("platform") != self.platform or reference.get("kind") != "video":
            raise AssistantError("unsupported_content", "B站模块当前读取视频引用")
        raw = await self.engine.video_context(reference["id"])
        return {"reference": reference, "title": raw["title"], "segments": raw.get("segments", []),
                "images": raw.get("images", []), "coverage": {k: v for k, v in raw["coverage"].items() if k != "bvid"},
                "source_url": raw.get("url", ""), "native": raw}

    @asynccontextmanager
    async def decision_material(self, event, *, guard):
        """Only this authenticated inbound message supplies private image inputs.

        Private image bytes live in a fresh temporary directory, not the shared
        public media cache. The isolated Codex broker copies explicit attachments;
        both copies are removed on success, cancellation or model failure.
        """
        import httpx

        from .media import download_image

        guard()
        _material_event(self.engine.store, event)
        settings = self.engine.store.read("companion")["data"]
        limit = min(4, settings.get("visuals_max_images", 4)) if settings.get("visuals_enabled", True) else 0
        attachments = list(event.attachments)
        context = {"cards": [item for item in attachments if item.get("kind") == "video_card"],
                   "images_available": sum(item.get("kind") == "image" for item in attachments),
                   "images_read": 0, "image_errors": [], "source": "current_inbound_event"}
        with tempfile.TemporaryDirectory(prefix="inbound-material-", dir=self.engine.store.runtime) as folder:
            isolated = SimpleNamespace(root=Path(folder).resolve())
            images = []
            for item in [a for a in attachments if a.get("kind") == "image"][:limit]:
                guard()
                try:
                    images.append(await download_image(isolated, item.get("url", "")))
                except AssistantError as exc:
                    if exc.code not in {"image_url", "image_format", "image_size", "invalid_image", "invalid_path"}:
                        raise
                    context["image_errors"].append(exc.code)
                except (httpx.HTTPError, OSError, ValueError):
                    context["image_errors"].append("image_unavailable")
                guard()
            context["images_read"] = len(images)
            yield {"context": context, "images": images}

    async def execute(self, capability, arguments, *, guard):
        guard()
        arguments = dict(arguments)
        event = arguments.pop("_event", None)
        retry_unsent = arguments.pop("_retry_unsent", False)
        disclosure = arguments.pop("_disclosure", None)
        if capability == "message.reply":
            if not event or event["platform"] != self.platform or event["account"] != self.account:
                raise AssistantError("ungrounded_target", "回复必须对应本账号的真实事件")
            kind = event["metadata"]["kind"]
            if kind not in ("reply", "dm_reply"):
                raise AssistantError("unsupported_event", "该事件无法回复")
            arguments["event_id"] = event["native_id"]
        elif capability in {c.name for c in self.capabilities()}:
            kind = capability.removeprefix("bilibili.")
        else:
            raise AssistantError("unknown_capability", "未声明该平台能力")
        try:
            prepared = self.engine.prepare(kind, arguments, retry_unsent=retry_unsent)
        except AssistantError as exc:
            return ActionResult("cancelled", error=exc.code)
        if disclosure and not prepared.get("duplicate"):
            self.engine.store.set_state("action-provenance:" + prepared["id"], disclosure)
        if prepared.get("duplicate") and prepared["status"] != "prepared":
            with self.engine.store.db() as db:
                row = db.execute("SELECT status,result FROM actions WHERE id=?", (prepared["id"],)).fetchone()
            # Preserve native deduplication across old and core generations, including uncertain sends.
            status = "uncertain" if row["status"] == "dispatching" else row["status"]
            receipt = (self.engine.store.ledger_decode("actions.result", row["result"]) if row["result"] else {})
            return ActionResult(status, {"legacy_action_id": prepared["id"], **receipt},
                                error=receipt.get("error", ""))
        try:
            result = await self.engine.execute(prepared["id"], guard=guard)
        except AssistantError as exc:
            with self.engine.store.db() as db:
                row = db.execute("SELECT status FROM actions WHERE id=?", (prepared["id"],)).fetchone()
                status = "cancelled" if row["status"] in ("prepared", "cancelled") else "uncertain"
                if row["status"] == "prepared":
                    db.execute("UPDATE actions SET status='cancelled',result=? WHERE id=?",
                               (self.engine.store.ledger_encode("actions.result", exc.as_dict()), prepared["id"]))
            return ActionResult(status, {"legacy_action_id": prepared["id"]}, error=exc.code)
        status = "uncertain" if result["status"] == "dispatching" else result["status"]
        return ActionResult(status, {"legacy_action_id": result["id"], **result.get("result", {})},
                            error=result.get("result", {}).get("error", ""))


def migrate_legacy(store):
    """Add normalized provenance; original files, action receipts and runtime stay authoritative."""
    from companion_core.people import bind_relationship
    account = store.read("manifest")["data"]["account_uid"]
    with store.db() as db:
        rows = db.execute("SELECT body FROM events WHERE account=?", (account,)).fetchall()
    count = 0
    for row in rows:
        item = store.ledger_decode("events.body", row[0])
        count += int(events.ingest(store, normalize(account, item))["new"])
    for item in store.records("relationships")[0]:
        bind_relationship(store, ActorRef("bilibili", item["data"]["id"]), item["data"]["id"])
    return {"normalized_events": count, "legacy_files_rewritten": 0, "role_started": False}
