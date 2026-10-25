from dataclasses import replace
from datetime import datetime, timezone

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import GovernedContext
from companion_core.interaction_growth import InteractionGrowth
from companion_core.public_workspace import PublicWorkspace
from tests_postgres.test_daily_service import started
from tests_postgres.test_runtime import Model


class Growing(Model):
    hook = None
    forged = False

    async def generate(self, instructions, context, schema, images=(), **options):
        if "queries" in schema["properties"]:
            return {"queries": [context["query"]]}
        result = await super().generate(instructions, context, schema, images, **options)
        result["growth_update"] = {"emotion": {"label": "private-feeling", "reason": "synthetic event",
            "valence_delta": .2, "arousal_delta": .2,
            "evidence_ids": ["forged" if self.forged else context["event"]["id"]]},
            "interests": [{"topic": "private-topic", "delta": .05, "quote": "synthetic"}],
            "reflection": {"content": "private-reflection", "traits": ["patient"],
                "speech_habits": [], "opinions": []} if context["private_growth"]["reflection_due"] else None}
        if self.hook:
            self.hook()
        return result


def prepare(store):
    service, token = started(store, daily_local_replies=100)
    companion = store.read("companion")
    store.write("companion", {**companion["data"], "reflection_hour": 0, "reflection_min_memories": 2},
                companion["version"], "fixture")
    model = Growing()
    service.core.decisions.model = model
    now = datetime.now(timezone.utc)
    service.core.clock = lambda: now
    return service, model, token


@pytest.mark.asyncio
async def test_private_growth_is_continuous_capped_and_reflects_with_same_reply_call(pg_store):
    service, model, token = prepare(pg_store)
    persona, growth = pg_store.read("persona"), pg_store.read("growth")
    for index in range(5):
        service.local.post(token, "synthetic", f"{index:032x}")
        await service.drain_local()
    assert len(model.contexts) == 5
    assert model.contexts[0]["current_emotion"]["valence"] == 0
    assert model.contexts[1]["current_emotion"]["valence"] == .2
    assert sum(bool(item["private_growth"]["reflection_due"]) for item in model.contexts) == 1
    assert model.contexts[-1]["private_growth"]["reflection"] == "private-reflection"
    latest = sorted(pg_store.records("interaction_growth")[0], key=lambda r: r["data"]["source"]["recorded_at"])
    assert len(latest) == 5
    assert all(item["data"]["state"].get("emotion", {}).get("change_today", 0) <= 1.2 for item in latest)
    assert pg_store.read("persona") == persona and pg_store.read("growth") == growth
    assert PublicWorkspace(pg_store).records("interaction_growth")[0] == []


@pytest.mark.asyncio
async def test_growth_never_crosses_actor_or_conversation_and_revocation_remains_live(pg_store):
    service, model, token = prepare(pg_store)
    received = service.local.post(token, "synthetic", "a" * 32)
    await service.drain_local()
    event = Event.from_dict(events.get(pg_store, received["id"])["event"])
    for other in (replace(event, conversation="another-private-conversation"),
                  replace(event, actor=ActorRef("local", "another-user"))):
        context = GovernedContext(pg_store, other)
        private = InteractionGrowth(service.core, context, other)
        assert private.context()["current_emotion"]["valence"] == 0
        assert private.context()["private_growth"]["interests"] == {}
    events.forget(pg_store, received["id"])
    service.local.post(token, "synthetic new", "b" * 32)
    await service.drain_local()
    assert model.contexts[-1]["current_emotion"]["valence"] == 0
    assert model.contexts[-1]["private_growth"]["interests"] == {}


@pytest.mark.asyncio
async def test_forged_growth_evidence_rolls_back_derived_memory_and_growth(pg_store):
    service, model, token = prepare(pg_store)
    model.forged = True
    received = service.local.post(token, "synthetic", "a" * 32)
    event = Event.from_dict(events.get(pg_store, received["id"])["event"])
    with pytest.raises(AssistantError, match="依据"):
        await service.core.handle(event)
    assert pg_store.records("interaction_growth")[0] == []
    assert pg_store.records("memories")[0] == []


@pytest.mark.asyncio
async def test_pause_during_decision_prevents_growth_commit(pg_store):
    service, model, token = prepare(pg_store)
    model.hook = lambda: service._pause("fixture")
    received = service.local.post(token, "synthetic", "a" * 32)
    with pytest.raises(AssistantError):
        await service.core.handle(Event.from_dict(events.get(pg_store, received["id"])["event"]))
    assert not pg_store.records("interaction_growth")[0]
    assert not pg_store.records("memories")[0]
