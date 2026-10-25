from dataclasses import replace
from datetime import datetime, timezone

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import GovernedContext
from companion_core.reply_trace import detail, listing
from companion_core.runtime import Companion
from companion_core.temporal_facts import PersonalFacts
from companion_service.admin import Management
from tests_postgres.test_runtime import Adapter, Host, Model
from tests_postgres.test_service_controls import prepared
from tests_postgres.test_web import PASSWORD


def facts(store, native="one", text="我住上海", user="a"):
    event = Event("alpha", "bot", native, ActorRef("alpha", user), "dm", datetime.now(timezone.utc).isoformat(), text)
    events.ingest(store, event)
    governed = GovernedContext(store, event)
    governed.classify_event()
    value = PersonalFacts(governed, event, lambda: datetime.now(timezone.utc))
    value.context()
    governed.freeze()
    return event, governed, value


def seed(store):
    event, governed, value = facts(store)
    assert value.save([{"attribute": "residence", "value": "上海", "quote": event.text,
                       "valid_from": None, "supersedes": []}])["written"] == 1
    return event, governed


def test_personal_facts_stay_encrypted_and_unreadable_after_source_revocation(pg_store):
    event, governed = seed(pg_store)
    assert len(facts(pg_store, "question", "住所")[2].context()["items"]) == 1
    assert not facts(pg_store, "question-other", "住所", user="other")[2].context()["items"]
    with pg_store.db() as db:
        payload = db.execute("SELECT body FROM content_records WHERE kind='memories'").fetchone()[0]
        assert "上海".encode() not in bytes(payload)
    policy = governed.repository.policy("event/" + event.id)
    governed.repository.set_policy(replace(policy, version="revoked-version", valid=False), policy.version, "test")
    assert not facts(pg_store, "question-after-revoke", "住所")[2].context()["items"]


@pytest.mark.asyncio
async def test_fact_change_during_generation_prevents_sending_and_is_traced(pg_store):
    seed(pg_store)
    class Changing(Model):
        async def generate(self, instructions, context, schema, images=(), **options):
            if "event" in context:
                item = context["personal_facts"]["items"][0]
                stored = pg_store.read("memories", item["id"])
                pg_store.write("memories", {**stored["data"], "content": "changed"}, stored["version"], "test", item["id"])
            return await super().generate(instructions, context, schema, images, **options)
    adapter = Adapter()
    core = Companion(pg_store, Changing(), Host(), [adapter])
    core.start()
    event, _, _ = facts(pg_store, "question", "我现在住哪里")
    with pytest.raises(AssistantError):
        await core.handle(event)
    assert not adapter.sent
    assert listing(pg_store)[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_trace_management_is_admin_only_and_sources_filtered_after_revocation(pg_store):
    seed(pg_store)
    core = Companion(pg_store, Model(), Host(), [Adapter()])
    core.start()
    event, governed, _ = facts(pg_store, "question", "住所")
    await core.handle(event)
    service = prepared(pg_store)
    management = Management(service)
    admin = service.sessions.request(service.sessions.login(PASSWORD, "admin"), "admin")
    role = service.sessions.request(service.sessions.login(PASSWORD, "role"), "role")
    with pytest.raises(AssistantError):
        management.operation(role, "reply_traces", {})
    trace = management.operation(admin, "reply_traces", {})[0]
    resource = "event/" + event.id
    policy = governed.repository.policy(resource)
    governed.repository.set_policy(replace(policy, valid=False, version="revoked"), policy.version, "test")
    shown = detail(pg_store, trace["id"], service.admin.access, admin)
    assert resource not in {source["resource"] for source in shown["sources"]}
    assert shown["hidden_sources"] >= 1
