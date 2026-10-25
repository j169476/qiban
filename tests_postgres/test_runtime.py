import json
from datetime import datetime, timezone

import pytest

from companion_core.contracts import ActionResult, ActorRef, Capability, Event
from companion_core.errors import AssistantError
from companion_core.governance import AdminService, resource_id
from companion_core.runtime import Companion
from companion_core.schema import obj


class Host:
    archived = False

    def identity(self):
        return "test-control"

    def require_active(self, thread_id=None):
        if self.archived:
            raise AssistantError("chat_archived", "synthetic archive")


class Adapter:
    platform, account = "alpha", "bot"

    def __init__(self):
        self.sent = []
        self.before = lambda: None

    def capabilities(self):
        return [Capability("message.reply", "reply", obj({"text": {"type": "string"}}))]

    async def execute(self, capability, arguments, *, guard):
        self.before()
        guard()
        self.sent.append(arguments)
        return ActionResult("succeeded", {"id": "synthetic"})


class Model:
    def __init__(self):
        self.contexts = []

    async def generate(self, instructions, context, schema, images=(), **options):
        self.contexts.append(context)
        return {"note": "", "action": {"capability": "message.reply", "arguments": {"text": "synthetic-reply"}},
                "memories": ["private-new-memory"], "identity_proposals": [], "relationship": None}


def prepare(store):
    service = AdminService(store)
    owner = ActorRef("local", "owner")
    admin = service.authority.issue(actor=owner, conversation="admin", audience="private", recipients=(owner,),
                                    purpose="management", administrator=True)
    for name, actor in (("self", ActorRef("alpha", "a")), ("other", ActorRef("alpha", "b"))):
        store.write("memories", {"schema_version": 1, "id": name, "kind": "experience",
                    "content": "草图 private-" + name, "source": {"type": "interaction", "reference": name,
                    "recorded_at": "2026-10-23T00:00:00Z"}, "confidence": "observed", "tags": [], "locked": False},
                    None, "test", name)
        resource = resource_id("memories", name)
        service.classify(admin, resource=resource, expected=service.repository.policy(resource).version,
                         classification="personal", owners=[actor.key])
    return service, admin


@pytest.mark.asyncio
async def test_full_core_private_context_and_encrypted_action(pg_store):
    prepare(pg_store)
    host, adapter, model = Host(), Adapter(), Model()
    core = Companion(pg_store, model, host, [adapter])
    core.start()
    event = Event("alpha", "bot", "message", ActorRef("alpha", "a"), "private-a",
                  datetime.now(timezone.utc).isoformat(), "草图", "private")
    result = await core.handle(event)
    assert result["action"]["status"] == "succeeded"
    payload = json.dumps(model.contexts, ensure_ascii=False)
    assert "private-self" in payload and "private-other" not in payload
    assert adapter.sent
    with pg_store.db() as db:
        body = db.execute("SELECT body FROM core_actions").fetchone()[0]
        assert "synthetic-reply" not in body
    assert (await core.handle(event))["duplicate"]
    host.archived = True
    with pytest.raises(AssistantError):
        core.guard()
    assert pg_store.state("core-run")["status"] == "paused"
    host.archived = False
    with pytest.raises(AssistantError):
        core.guard()


@pytest.mark.asyncio
async def test_governed_reply_reuses_scoped_query_expansion_without_supplying_other_memories(pg_store):
    prepare(pg_store)
    settings = pg_store.read("companion")
    pg_store.write("companion", {**settings["data"], "reflection_min_memories": 1}, settings["version"], "fixture")

    class Expanding(Model):
        async def generate(self, instructions, context, schema, images=(), **options):
            if "queries" in schema["properties"]:
                self.contexts.append(context)
                assert set(context) == {"query"}
                assert "private-self" not in str(context) and "private-other" not in str(context)
                return {"queries": ["草图"]}
            return await super().generate(instructions, context, schema, images, **options)

    model, adapter = Expanding(), Adapter()
    core = Companion(pg_store, model, Host(), [adapter])
    core.start()
    event = Event("alpha", "bot", "expanded", ActorRef("alpha", "a"), "private-a",
                  datetime.now(timezone.utc).isoformat(), "那份线条画", "private")
    result = await core.handle(event)
    assert result["action"]["status"] == "succeeded"
    assert model.contexts[0] == {"query": "那份线条画"}
    decision = model.contexts[1]
    assert "private-self" in str(decision) and "private-other" not in str(decision)
    core.stop()


@pytest.mark.asyncio
async def test_revoke_during_transport_preparation_cancels_send(pg_store):
    service, admin = prepare(pg_store)
    adapter, model = Adapter(), Model()
    core = Companion(pg_store, model, Host(), [adapter])
    core.start()

    def invalidate():
        policy = service.repository.policy("memories/self")
        service.classify(admin, resource=policy.resource, expected=policy.version,
                         classification="restricted", owners=policy.owners)

    adapter.before = invalidate
    event = Event("alpha", "bot", "message", ActorRef("alpha", "a"), "private-a",
                  datetime.now(timezone.utc).isoformat(), "草图", "private")
    result = await core.handle(event)
    assert result["action"]["status"] == "cancelled"
    assert not adapter.sent


@pytest.mark.asyncio
async def test_successive_relationship_updates_preserve_private_dialogue(pg_store):
    class Growing(Model):
        async def generate(self, *args, **kwargs):
            result = await super().generate(*args, **kwargs)
            result["relationship"] = {"delta": 1, "impression": "一起讨论草图", "facts": ["喜欢画草图"]}
            return result

    adapter, model = Adapter(), Growing()
    core = Companion(pg_store, model, Host(), [adapter])
    core.start()
    for index in range(3):
        before = pg_store.version("relationships", ActorRef("alpha", "a").key)
        event = Event("alpha", "bot", "turn-" + str(index), ActorRef("alpha", "a"), "private-a",
                      datetime.now(timezone.utc).isoformat(), "一起讨论草图 " + str(index), "private")
        try:
            result = await core.handle(event)
        except AssistantError:
            # Failed scope validation must not partially commit affinity changes.
            assert pg_store.version("relationships", ActorRef("alpha", "a").key) == before
            raise
        assert result["action"]["status"] == "succeeded"
        assert (await core.handle(event))["duplicate"]
    assert len(adapter.sent) == 3
    assert len(model.contexts[-1]["conversation"]["turns"]) == 5
    relationship = pg_store.read("relationships", ActorRef("alpha", "a").key)["data"]
    assert relationship["affinity"] == 53 and relationship["interactions"] == 3


@pytest.mark.asyncio
async def test_aggregate_scope_commit_failure_rolls_back_relationship_receipt_and_memory(pg_store, monkeypatch):
    from companion_core.governance import GovernedContext
    from companion_core.relationship_store import receipt

    class Growing(Model):
        async def generate(self, *args, **kwargs):
            result = await super().generate(*args, **kwargs)
            result["relationship"] = {"delta": 1, "impression": "synthetic", "facts": []}
            return result

    adapter = Adapter()
    core = Companion(pg_store, Growing(), Host(), [adapter])
    core.start()
    event = Event("alpha", "bot", "atomic", ActorRef("alpha", "a"), "private-a",
                  datetime.now(timezone.utc).isoformat(), "synthetic", "private")
    original = GovernedContext.classify_written

    def fail(self, kind, *args, **kwargs):
        original(self, kind, *args, **kwargs)
        if kind == "relationships":
            raise RuntimeError("synthetic provenance commit failure")

    monkeypatch.setattr(GovernedContext, "classify_written", fail)
    with pytest.raises(RuntimeError, match="provenance commit failure"):
        await core.handle(event)
    assert not pg_store.exists("relationships", event.actor.key)
    assert pg_store.state(receipt(event.actor.key, event.id)) is None
    assert not pg_store.records("memories")[0]
    assert not adapter.sent
    monkeypatch.setattr(GovernedContext, "classify_written", original)
    assert (await core.handle(event))["action"]["status"] == "succeeded"
    assert (await core.handle(event))["duplicate"]
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["after_retrieval", "after_model", "after_write", "before_send"])
async def test_revocation_at_each_disclosure_boundary_never_sends_old_material(pg_store, monkeypatch, phase):
    from companion_core.governance import GovernedContext

    service, admin = prepare(pg_store)
    fired = False

    def revoke():
        nonlocal fired
        if fired:
            return
        fired = True
        policy = service.repository.policy("memories/self")
        service.classify(admin, resource=policy.resource, expected=policy.version,
                         classification="restricted", owners=policy.owners)

    class Revoked(Model):
        async def generate(self, *args, **kwargs):
            if phase == "after_retrieval":
                revoke()
            result = await super().generate(*args, **kwargs)
            result["relationship"] = {"delta": 1, "impression": "synthetic", "facts": []}
            if phase == "after_model":
                revoke()
            return result

    adapter = Adapter()
    if phase == "before_send":
        adapter.before = revoke
    if phase == "after_write":
        original = GovernedContext.classify_written

        def after_write(scope, kind, *args, **kwargs):
            original(scope, kind, *args, **kwargs)
            if kind == "relationships":
                revoke()

        monkeypatch.setattr(GovernedContext, "classify_written", after_write)
    core = Companion(pg_store, Revoked(), Host(), [adapter])
    core.start()
    event = Event("alpha", "bot", "revocation-" + phase, ActorRef("alpha", "a"), "private-a",
                  datetime.now(timezone.utc).isoformat(), "草图", "private")
    if phase == "before_send":
        assert (await core.handle(event))["action"]["status"] == "cancelled"
    else:
        with pytest.raises(AssistantError):
            await core.handle(event)
        assert not pg_store.exists("relationships", event.actor.key)
        assert len(pg_store.records("memories")[0]) == 2
    assert fired and not adapter.sent
