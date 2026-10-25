"""Synthetic PostgreSQL, authenticated sessions and the real scheduling/ACL path."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_core import budget, conversations, events
from companion_core.access import RecordPolicy
from companion_core.contracts import ActorRef, Event
from companion_core.conversation_maintenance import ConversationMaintenance
from companion_core.governance import GovernedContext, ReadView
from companion_core.memory_maintenance import best_summary
from companion_service.service import Service
from tests_postgres.test_web import PASSWORD, Host, client, login


class Model:
    def __init__(self):
        self.contexts = []
        self.summaries = []
        self.hook = None

    async def generate(self, instructions, context, schema, images=(), **options):
        if "summary" in schema["properties"]:
            self.summaries.append(context)
            if self.hook:
                self.hook()
            previous = context["previous_summary"]
            # Small deterministic test oracle: preserve a distant fact while
            # exercising real cumulative summaries, without a live model call.
            text = (previous["content"] if previous else "") + " ".join(
                item["data"]["content"] for item in context["turns"])
            return {"summary": "约定：蓝色纸船；纠正后时间为周五。" if "蓝色纸船" in text else "合成对话摘要"}
        self.contexts.append(context)
        return {"note": "synthetic", "action": {"capability": "message.reply", "arguments": {"text": "synthetic-reply"}},
                "memories": [], "identity_proposals": []}


def prepare(store, **settings):
    model = Model()
    service = Service(store, model, Host(), bridge_secret="synthetic-bridge-secret")
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    token = service.sessions.login(PASSWORD, "role")
    admin = service.sessions.request(service.sessions.login(PASSWORD, "admin"), "admin")
    service.admin.patch_config(admin, "companion", {"conversation_summary_after": 12, "conversation_keep_recent": 4,
        **settings}, store.version("companion"))
    service.admin.patch_config(admin, "policy", {"daily_model_cycles": 1000}, store.version("policy"))
    service.control("synthetic-bridge-secret", "control", "assistant_start", {"background": False})
    now = [datetime.now(timezone.utc)]
    service.core.clock = lambda: now[0]
    service.wakeup.clear()
    return service, model, token, admin, now


def seed(service, count, *, start=0, conversation=None, content=None, speaker=None):
    store = service.store
    actor = ActorRef(**store.state("web-owner")["actor"])
    conversation = conversation or service.local.conversation()
    tid = conversations.thread_id(service.local.account, "local", conversation)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = []
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        for i in range(start, start + count):
            text = content if content is not None else ("蓝色纸船；纠正后时间为周五" if i == 0 else f"synthetic-{i}")

            def classify(identifier):
                resource = "conversation_turns/" + identifier
                old = service.admin.repository.policy(resource)
                service.admin.repository.set_policy(RecordPolicy(resource, "synthetic-" + identifier, "conversation",
                    frozenset((actor.key,)), conversation, "private"), old.version, "synthetic")

            author = speaker or ("user" if i % 2 == 0 else "assistant")
            ids.extend(conversations.append(store, service.local.account, "local", conversation, f"synthetic-{i:05}",
                text, author, actor.key, "received" if author == "user" else "sent",
                (stamp + timedelta(seconds=i)).isoformat(), on_write=classify))
    return tid, ids


def advance(now, seconds=31):
    now[0] += timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_web_reply_triggers_automatic_idle_compression_and_recall(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    role = client(service, "role")
    headers = login(role, service, "role")
    for i in range(8):
        response = role.post("/messages", json={"text": "蓝色纸船；纠正后时间为周五" if i == 0 else f"hello-{i}",
                                               "nonce": f"{i:032x}"}, headers=headers)
        assert response.status_code == 202
    # Exercise the actual idle worker, with browsing explicitly disabled.
    task = asyncio.create_task(service.work())
    try:
        async with asyncio.timeout(30):
            while not model.summaries or not pg_store.records("conversation_summaries")[0]:
                if len(model.contexts) == 8:
                    advance(now)
                await asyncio.sleep(.02)
    finally:
        service.shutdown.set()
        service.wakeup.set()
        await task
    assert len(model.contexts) == 8 and len(model.summaries) == 1
    assert len(service.local.history(token)) == 16
    response = role.post("/messages", json={"text": "之前约定的颜色和更正时间？", "nonce": "f" * 32}, headers=headers)
    event = Event.from_dict(events.get(pg_store, response.json()["id"])["event"])
    await service.core.handle(event)
    assert "蓝色纸船" in model.contexts[-1]["conversation"]["summary"]["content"]
    assert "周五" in model.contexts[-1]["conversation"]["summary"]["content"]
    assert service.status()["status"] == "active"
    assert pg_store.records("memories")[0] == []


@pytest.mark.asyncio
async def test_scope_filter_and_raw_history_preservation(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    tid, ids = seed(service, 16)
    seed(service, 16, conversation="unrelated-private-thread", content="FOREIGN_PRIVATE_CANARY")
    # Even an authorized different conversation cannot enter this job.
    foreign = ActorRef("local", "another-user")
    event = Event("local", service.local.account, "probe", foreign, foreign.key, "", "")
    assert not GovernedContext(pg_store, event).conversation(service.local.account, tid)["turns"]
    outcome = await service.maintain_local()
    assert outcome["status"] == "saved"
    assert "FOREIGN_PRIVATE_CANARY" not in json.dumps(model.summaries)
    policy = service.admin.repository.policy("conversation_summaries/" + outcome["saved"])
    assert policy.classification == "conversation" and policy.audience == "private"
    assert policy.conversation == service.local.conversation()
    assert len(policy.sources) == 12 and len(pg_store.records("conversation_turns")[0]) == 32
    outsider = GovernedContext(pg_store, event)
    assert not outsider.access.can_read(outsider.request, policy.resource)
    assert await service.maintain_local() is None
    advance(now)
    assert await service.maintain_local() is None
    assert len(model.summaries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["logout", "pause", "archive", "revoke", "edit", "settings", "recreate", "session_actor", "session_purpose"])
async def test_mid_generation_change_discards_summary(pg_store, change):
    service, model, token, admin, now = prepare(pg_store)
    tid, ids = seed(service, 16)
    before = pg_store.read("persona")

    def mutate():
        resource = "conversation_turns/" + ids[0]
        policy = service.admin.repository.policy(resource)
        if change == "logout":
            service.sessions.logout(token)
        elif change == "pause":
            service._pause("explicit_stop")
        elif change == "archive":
            service.host.archived = True
        elif change == "revoke":
            service.admin.repository.set_policy(replace(policy, version="revoked", valid=False), policy.version, "test")
        elif change == "settings":
            service.admin.patch_config(admin, "companion", {"memory_maintenance": False}, pg_store.version("companion"))
        elif change in {"session_actor", "session_purpose"}:
            with pg_store.db() as db:
                column, value = ("actor", "different") if change == "session_actor" else ("purpose", "admin")
                db.execute("UPDATE governance_sessions SET " + column + "=? WHERE purpose='role'", (value,))
        else:
            row = pg_store.read("conversation_turns", ids[0])
            if change == "recreate":
                with pg_store.db() as db:
                    db.execute("DELETE FROM content_records WHERE kind='conversation_turns' AND id=?", (ids[0],))
                expected = None
            else:
                expected = row["version"]
                row["data"]["content"] = "corrected"
            pg_store.write("conversation_turns", row["data"], expected, "test", ids[0])

    model.hook = mutate
    result = await service.maintain_local()
    assert result["status"] == "deferred"
    assert not pg_store.records("conversation_summaries")[0]
    assert pg_store.read("persona") == before


@pytest.mark.asyncio
async def test_atomic_summary_policy_commit_and_failure_backoff(pg_store, monkeypatch):
    service, model, token, admin, now = prepare(pg_store)
    seed(service, 16)
    import companion_core.conversation_maintenance as module

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_PROMPT_CANARY")

    monkeypatch.setattr(module, "record_provenance", fail)
    for attempt in range(3):
        result = await service.maintain_local()
        assert result["failures"] == attempt + 1
        assert not pg_store.records("conversation_summaries")[0]
        status = service.compression.status(service.local.account, "local", service.local.conversation())
        assert "PRIVATE_PROMPT_CANARY" not in json.dumps(status)
        assert await service.maintain_local() is None
        advance(now, 61 * 2 ** attempt)
    assert len(model.summaries) == 3 and service.status()["status"] == "paused"
    fault = pg_store.state("fault:" + pg_store.state("core-run")["generation"])
    assert fault["code"] == "private_compression_failed" and fault["notification"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", " \n\t "])
async def test_empty_summary_does_not_mark_sources_as_covered(pg_store, content):
    service, model, token, admin, now = prepare(pg_store)
    seed(service, 16)

    async def generate(*args, **kwargs):
        return {"summary": content}

    model.generate = generate
    assert (await service.maintain_local())["status"] == "failed"
    assert len(pg_store.records("conversation_turns")[0]) == 16
    assert not pg_store.records("conversation_summaries")[0]
    assert budget.status(pg_store, pg_store.read("policy")["data"], now[0])["used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["paused", "read_only", "logged_out", "disabled", "quota", "priority"])
async def test_scheduling_gates_prevent_model_use(pg_store, blocked):
    service, model, token, admin, now = prepare(pg_store)
    seed(service, 16)
    if blocked == "paused":
        service._pause("explicit_stop")
    elif blocked == "read_only":
        service._pause("explicit_stop")
        service.control("synthetic-bridge-secret", "control", "assistant_start", {"read_only": True})
        service.wakeup.clear()
    elif blocked == "logged_out":
        service.sessions.logout(token)
    elif blocked == "disabled":
        service.admin.patch_config(admin, "companion", {"memory_maintenance": False}, pg_store.version("companion"))
    elif blocked == "quota":
        service.admin.patch_config(admin, "policy", {"daily_model_cycles": 0}, pg_store.version("policy"))
    else:
        service.wakeup.set()
    assert await service.maintain_local() is None
    assert not model.summaries and not pg_store.records("conversation_summaries")[0]


@pytest.mark.asyncio
async def test_restart_requires_start_and_session_then_continues_chain(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    tid, ids = seed(service, 16)
    first = await service.maintain_local()
    seed(service, 12, start=16)
    restarted = Service(pg_store, model, Host(), bridge_secret="synthetic-bridge-secret")
    await restarted.open()
    await restarted.close()
    assert await restarted.maintain_local() is None
    assert len(model.summaries) == 1
    restarted.control("synthetic-bridge-secret", "control", "assistant_start", {"background": False})
    restarted.wakeup.clear()
    second = await restarted.maintain_local()
    summary = pg_store.read("conversation_summaries", second["saved"])
    assert summary["data"]["previous"]["id"] == first["saved"]
    assert len(conversations.records(pg_store, service.local.account, tid)) == 28
    original = pg_store.read("conversation_turns", ids[0])
    pg_store.write("conversation_turns", {**original["data"], "content": "manual correction"}, original["version"], "test", ids[0])
    request = restarted.local.maintenance_request()
    allowed = restarted.admin.repository.readable_resources(restarted.compression.access, request)
    assert not best_summary(ReadView(pg_store, allowed), service.local.account, tid)


@pytest.mark.asyncio
async def test_revoked_summary_is_not_republished_or_recomputed(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    seed(service, 16)
    saved = await service.maintain_local()
    resource = "conversation_summaries/" + saved["saved"]
    policy = service.admin.repository.policy(resource)
    service.admin.repository.set_policy(replace(policy, version="revoked", valid=False), policy.version, "test")
    advance(now)
    assert (await service.maintain_local())["status"] == "deferred"
    assert not service.admin.repository.policy(resource).valid
    assert len(model.summaries) == 1


@pytest.mark.asyncio
async def test_long_turn_partial_spans_never_claim_unread_tail(pg_store):
    service, model, token, admin, now = prepare(pg_store, context_char_limit=4000)
    tid, ids = seed(service, 16, content="synthetic-long-" * 800)
    first = await service.maintain_local()
    record = pg_store.read("conversation_summaries", first["saved"])
    span = record["data"]["turn_spans"][0]
    assert 0 < span["end"] < span["total"]
    assert len(json.dumps(model.summaries[-1], ensure_ascii=False)) <= 4000
    prior_end = span["end"]
    advance(now)
    second = await service.maintain_local()
    assert pg_store.read("conversation_summaries", second["saved"])["data"]["turn_spans"][0]["start"] == prior_end
    assert len(conversations.records(pg_store, service.local.account, tid)) == 16


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", [100, 500, 1000])
async def test_large_conversation_complete_chain_and_recall(pg_store, rounds):
    service, model, token, admin, now = prepare(pg_store, context_char_limit=60000)
    tid, ids = seed(service, rounds * 2)
    while True:
        outcome = await service.maintain_local()
        if outcome is None:
            break
        assert outcome["status"] == "saved", outcome
        advance(now)
    request = service.local.maintenance_request()
    view = service.compression._view(request)
    compressed = best_summary(view, service.local.account, tid)
    assert compressed and len(compressed["covered"]) == rounds * 2 - 4
    context = conversations.context(view, service.local.account, tid)
    assert not context["truncated"] and len(context["turns"]) == 4
    assert "蓝色纸船" in context["summary"]["content"] and "周五" in context["summary"]["content"]
    assert len(conversations.records(pg_store, service.local.account, tid)) == rounds * 2
    assert all(len(item["turns"]) <= 50 for item in model.summaries)
    assert len(model.summaries) <= (rounds * 2 + 49) // 50
    policy = service.admin.repository.policy("conversation_summaries/" + compressed["record"]["data"]["id"])
    assert len(policy.sources) <= 51
    lease = service.compression.access.lease(request, [policy.resource])
    assert set("conversation_turns/" + identifier for identifier in ids[:-4]) <= dict(lease.versions).keys()
    old = service.admin.repository.policy("conversation_turns/" + ids[0])
    service.admin.repository.set_policy(replace(old, version="revoked", valid=False), old.version, "test")
    assert not service.compression.access.can_read(request, policy.resource)


@pytest.mark.asyncio
async def test_background_model_checkpoint_prioritizes_new_message(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    seed(service, 16)
    service.local.post(token, "interactive-first", "e" * 32)
    assert (await service.maintain_local())["status"] == "saved"
    assert model.contexts[0]["event"]["text"] == "interactive-first"
    assert len(service.local.history(token)) == 18


@pytest.mark.asyncio
async def test_shared_quota_and_interrupted_claim_recovery(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    seed(service, 16)
    key = service.compression.key(service.local.account, "local", service.local.conversation())
    original = service.core.model

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()

    service.core.model = interrupted
    with pytest.raises(asyncio.CancelledError):
        await service.maintain_local()
    assert pg_store.state(key)["status"] == "running"
    assert not pg_store.records("conversation_summaries")[0]
    service.core.model = original
    # A second scheduler cannot concurrently steal the current generation's claim.
    service.compression = ConversationMaintenance(service.core, service.admin.authority)
    assert await service.maintain_local() is None
    service._pause("explicit_stop")
    service.control("synthetic-bridge-secret", "control", "assistant_start", {"background": False})
    service.wakeup.clear()
    assert (await service.maintain_local())["status"] == "saved"
    assert budget.status(pg_store, pg_store.read("policy")["data"], now[0])["used"] == 1
    assert len(model.summaries) == 1


@pytest.mark.asyncio
async def test_deleting_source_invalidates_saved_chain_and_future_recall(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    tid, ids = seed(service, 16)
    result = await service.maintain_local()
    with pg_store.db() as db:
        db.execute("DELETE FROM content_records WHERE kind='conversation_turns' AND id=?", (ids[0],))
    view = service.compression._view(service.local.maintenance_request())
    assert best_summary(view, service.local.account, tid) is None
    assert conversations.context(view, service.local.account, tid)["summary"] is None
    assert pg_store.exists("conversation_summaries", result["saved"])


def test_only_authenticated_admin_sees_compression_status(pg_store):
    service, model, token, admin, now = prepare(pg_store)
    admin_client, role = client(service, "admin"), client(service, "role")
    assert admin_client.get("/status").status_code == 401
    login(admin_client, service, "admin")
    login(role, service, "role")
    assert "compression" in admin_client.get("/status").json()
    assert "compression" not in role.get("/status").json()
