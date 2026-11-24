"""Linux worker. The transport endpoint and model are deterministic, local fixtures."""
import asyncio
import fcntl
import uuid
from contextlib import closing

import dialogue_store as fixture
import jobs
from service import Service

from companion_core import delivery, events
from companion_core.contracts import ActionResult, Capability, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations
from companion_core.runtime import Companion
from companion_core.schema import obj


class Host:
    def __init__(self, instance):
        self.instance = instance

    def identity(self):
        return self.instance

    def require_active(self, owner=None):
        if owner is not None and owner != self.instance:
            raise AssistantError("stale_runtime", "Inactive synthetic worker")


class Model:
    async def generate(self, instructions, context, output_schema, images=(), **options):
        identifier = context["event"]["native_id"]
        with fixture.connection() as db:
            row = db.execute("UPDATE lab_messages SET model_calls=model_calls+1,in_model=true WHERE id=%s "
                             "RETURNING mode", (identifier,)).fetchone()
        if row["mode"] == "hold":
            for _ in range(300):
                with fixture.connection() as db:
                    if db.execute("SELECT released FROM lab_messages WHERE id=%s", (identifier,)).fetchone()["released"]:
                        break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Fixture model gate timed out")
        return {"note": "", "memories": [], "identity_proposals": [], "relationship": None,
                "action": {"capability": "message.reply", "arguments": {"text": fixture.REPLY}}}


class Transport:
    platform, account = "qq", fixture.SCOPE.account_identity

    def __init__(self, store):
        self.permissions = ReplyAuthorizations(store)

    def capabilities(self):
        return [Capability("message.reply", "回复本地合成私聊", obj({"text": {"type": "string", "maxLength": 300}}))]

    def validate_event(self, event):
        if (event.account != self.account or event.platform != self.platform or event.visibility != "private"
                or event.actor.user_id != fixture.SCOPE.target_id
                or event.metadata["qq"]["target_id"] != fixture.SCOPE.target_id):
            raise AssistantError("connector_scope", "Unexpected synthetic conversation")
        self.permissions.check(fixture.SCOPE, event.metadata["fixture_permit"])

    async def execute(self, capability, arguments, *, guard):
        event = Event.from_dict(arguments["_event"])
        self.validate_event(event)
        # Hold the same lock as actual revocation until the independent receiver commits its submission.
        with self.permissions.submission(fixture.SCOPE, event.metadata["fixture_permit"]):
            guard()
            receipt = uuid.uuid4().hex
            with fixture.connection() as receiver:
                row = receiver.execute("SELECT mode FROM lab_messages WHERE id=%s", (event.native_id,)).fetchone()
                receiver.execute("INSERT INTO lab_receiver(message,target,body,receipt) VALUES (%s,%s,%s,%s)",
                                 (event.native_id, fixture.SCOPE.target_id, arguments["text"], receipt))
        if row["mode"] == "interrupt":
            # Independent durable receiver has accepted; the acceptance harness restarts us at this point.
            await asyncio.sleep(60)
            raise TimeoutError("fixture receipt withheld")
        if row["mode"] == "lost":
            raise TimeoutError("fixture receipt lost")
        return ActionResult("succeeded", {"id": receipt})


class DialogueService(Service):
    def __init__(self, sqlite, store):
        super().__init__(sqlite)
        self.store = store
        self.transport = Transport(store)
        self.core = Companion(store, Model(), Host(self.instance), [self.transport], run_key=fixture.RUN_KEY)

    def dispatch(self, req, negotiated):
        response, negotiated = super().dispatch(req, negotiated)
        if response["ok"] and req["operation"] == "runtime.status":
            result = response["result"]
            platforms = result["platforms"]
            result["platforms"] = {**platforms, "local": {**platforms["local"], "reason": "会客室尚未接入。"}}
            result["synthetic_dialogue"] = fixture.progress()
        return response, negotiated

    def recover_uncertain(self, event, version):
        """Finish a prior ambiguous submission without asking for another decision."""
        key = stable_key("dispatch", event.platform, event.account, event.id, "message.reply")
        with self.store.lock, self.store.db() as db:
            if self.store.state("core-processed:" + event.id, db=db) == version:
                return None  # Normal replay goes through Companion's own receipt check.
            prior = db.execute("SELECT id,body,generation FROM core_actions WHERE dedupe=? AND status='uncertain'",
                               (key,)).fetchone()
            if not prior:
                return None
            run = self.core.guard()
            body = self.store.ledger_decode("core_actions.body", prior["body"])
            if prior["generation"] == run["generation"] or Event.from_dict(body["event"]) != event:
                return None
            delivery.finish(self.core, event, version, run, reason="receipt_unknown")
            return {"action": {"id": prior["id"], "status": "uncertain"}}

    async def process(self):
        with fixture.connection() as db:
            row = db.execute("SELECT i.id AS input,m.* FROM lab_inputs i JOIN lab_messages m ON m.id=i.message "
                             "WHERE i.outcome='queued' ORDER BY i.id LIMIT 1").fetchone()
            if not row:
                return
            db.execute("UPDATE lab_inputs SET outcome='processing' WHERE id=%s", (row["input"],))
        event = fixture.make_event(row)
        error, action_id = None, None
        try:
            self.transport.validate_event(event)
            received = events.ingest(self.store, event)
            result = self.recover_uncertain(event, received["version"]) or await self.core.handle(event)
            action = result.get("action") or {}
            action_id = action.get("id")
            outcome = "duplicate" if result.get("duplicate") else action.get("status", "no_action")
        except AssistantError as exc:
            error = exc.code
            outcome = "denied" if error == "reply_not_authorized" else "failed"
        with fixture.connection() as db:
            db.execute("UPDATE lab_inputs SET outcome=%s,error=%s,action=%s WHERE id=%s",
                       (outcome, error, action_id, row["input"]))

    async def consume(self):
        while True:
            await self.process()
            await asyncio.sleep(0.2)

    async def run(self):
        self.core.start()
        consumer = asyncio.create_task(self.consume())
        status = asyncio.create_task(super().run())
        try:
            done, _ = await asyncio.wait((status, consumer), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (consumer, status):
                task.cancel()
            await asyncio.gather(consumer, status, return_exceptions=True)
            self.core.stop()


if __name__ == "__main__":
    with open("/var/lib/qiban/worker.lock", "a") as lock, closing(jobs.connect()) as sqlite:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = fixture.open_store(provision=True)
        try:
            fixture.initialize_transport()
            asyncio.run(DialogueService(sqlite, store).run())
        finally:
            store.database.close()
