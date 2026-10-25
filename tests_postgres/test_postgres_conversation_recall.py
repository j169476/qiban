import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_core import conversations, events
from companion_core.contracts import ActionResult, ActorRef, Capability, Event
from companion_core.errors import AssistantError
from companion_core.governance import AdminService, GovernedContext
from companion_core.runtime import Companion
from companion_core.schema import obj


def event(index, text, *, account="bot", thread="private-a", actor="a"):
    return Event("alpha", account, f"message-{index}", ActorRef("alpha", actor), thread,
                 (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat(),
                 text, "private")


def record(store, source):
    events.ingest(store, source)
    governed = GovernedContext(store, source)
    governed.classify_event()
    identifiers = conversations.append(store, source.account, source.platform, source.thread, source.id,
        source.text, "user", source.actor.key, "received", source.occurred_at, source.occurred_at,
        on_write=lambda key: governed.classify_written("conversation_turns", key, sources=["event/" + source.id]))
    return identifiers[0]


def seed(store, *, long=False):
    settings = store.read("companion")
    store.write("companion", {**settings["data"], "conversation_context_turns": 4}, settings["version"], "fixture")
    sources, keys = [], []
    with store.lock, store.db():
        for index in range(32):
            content = f"今天看看天气。记录 {index}"
            if index == 4:
                content = ("无关背景。" * 1200 if long else "") + "北岸灯塔的碰面暗号是钴蓝纸鹤，约在周四。"
            if index == 5:
                content = "更正刚才的约定：改为周五，周四取消。"
            sources.append(event(index, content))
            keys.append(record(store, sources[-1]))
    return sources, keys


def restrict(store, resource):
    service = AdminService(store)
    owner = ActorRef("local", "owner")
    admin = service.authority.issue(actor=owner, conversation="admin", audience="private", recipients=(owner,),
                                    purpose="management", administrator=True)
    policy = service.repository.policy(resource)
    service.classify(admin, resource=resource, expected=policy.version,
                     classification="restricted", owners=policy.owners)


def test_permissions_and_thread_scope_filter_before_decryption_and_scoring(pg_store, monkeypatch):
    from companion_core import conversation_recall

    _, keys = seed(pg_store)
    denied = record(pg_store, event(100, "DENIED_CANARY 北岸灯塔"))
    restrict(pg_store, "conversation_turns/" + denied)
    foreign = [record(pg_store, event(101, "OTHER_ACCOUNT_CANARY 北岸灯塔", account="other")),
               record(pg_store, event(102, "OTHER_THREAD_CANARY 北岸灯塔", thread="private-b"))]
    stale_source = event(103, "STALE_SOURCE_CANARY 北岸灯塔")
    stale = record(pg_store, stale_source)
    events.ingest(pg_store, replace(stale_source, text="corrected source"))
    forbidden = {denied, stale, *foreign}
    decoded, ranked = [], []
    original_decode, original_scores = pg_store._decode, conversation_recall.scores

    def decode(kind, key, *args):
        if kind == "conversation_turns":
            decoded.append(key)
        return original_decode(kind, key, *args)

    def scores(records, *args, **kwargs):
        ranked.extend(row["data"]["id"] for row in records)
        return original_scores(records, *args, **kwargs)

    monkeypatch.setattr(pg_store, "_decode", decode)
    monkeypatch.setattr(conversation_recall, "scores", scores)
    current = event(200, "北岸灯塔")
    governed = GovernedContext(pg_store, current)
    result = governed.conversation("bot", conversations.thread_id("bot", "alpha", current.thread), query="北岸灯塔")
    assert keys[4] in ranked and "钴蓝纸鹤" in str(result["recalled"])
    assert not forbidden.intersection(decoded) and not forbidden.intersection(ranked)
    assert "CANARY" not in str(result)
    governed.freeze().check()
    pins = dict(governed.frozen_sources)
    for item in result["recalled"]:
        key = item["data"]["id"]
        assert "conversation_turns/" + key in pins
        assert governed.versions["conversation_turns", key] == item["version"]
    assert not list((pg_store.root / "cache").rglob("*.sqlite*"))


class Host:
    def identity(self):
        return "test-control"

    def require_active(self, thread_id=None):
        return None


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
    def __init__(self, *, fail_expansion=False):
        self.expansions, self.decisions = [], []
        self.fail_expansion = fail_expansion

    async def generate(self, instructions, context, schema, images=(), **options):
        if "queries" in schema["properties"]:
            self.expansions.append(context)
            assert set(context) == {"query"}
            if self.fail_expansion:
                raise AssistantError("model_failed", "synthetic expansion failure")
            return {"queries": ["北岸灯塔"]}
        self.decisions.append(context)
        assert "conversation.recalled" in instructions
        return {"note": "", "action": {"capability": "message.reply", "arguments": {"text": "synthetic-reply"}},
                "memories": [], "identity_proposals": [], "relationship": None}


@pytest.mark.asyncio
async def test_history_only_uses_one_shared_expansion_and_scoped_plan_cache(pg_store):
    seed(pg_store)
    assert not pg_store.records("memories")[0]
    model, adapter = Model(), Adapter()
    core = Companion(pg_store, model, Host(), [adapter])
    core.start()
    try:
        for index in (200, 201):
            result = await core.handle(event(index, "之前那个碰面安排"))
            assert result["action"]["status"] == "succeeded"
            conversation = model.decisions[-1]["conversation"]
            assert "钴蓝纸鹤" in str(conversation["recalled"]) and "周五" in str(conversation["recalled"])
            assert conversation["context_chars"] <= 12000
        assert model.expansions == [{"query": "之前那个碰面安排"}]
        assert len(model.decisions) == len(adapter.sent) == 2
    finally:
        core.stop()


@pytest.mark.asyncio
async def test_failed_expansion_falls_back_to_original_history_query(pg_store):
    seed(pg_store)
    model, adapter = Model(fail_expansion=True), Adapter()
    core = Companion(pg_store, model, Host(), [adapter])
    core.start()
    try:
        assert (await core.handle(event(200, "北岸灯塔")))["action"]["status"] == "succeeded"
        assert "钴蓝纸鹤" in str(model.decisions[-1]["conversation"]["recalled"])
        assert len(model.expansions) == len(model.decisions) == 1
    finally:
        core.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("disable", [False, True])
async def test_budget_fallback_and_recall_switch_do_not_spend_an_extra_model_call(pg_store, disable):
    seed(pg_store)
    if disable:
        settings = pg_store.read("companion")
        pg_store.write("companion", {**settings["data"], "conversation_recall_enabled": False},
                       settings["version"], "fixture")
    else:
        policy = pg_store.read("policy")
        pg_store.write("policy", {**policy["data"], "daily_model_cycles": 1}, policy["version"], "fixture")
    model, adapter = Model(), Adapter()
    core = Companion(pg_store, model, Host(), [adapter])
    core.start()
    try:
        result = await core.handle(event(200, "北岸灯塔"))
        assert result["action"]["status"] == "succeeded"
        assert not model.expansions and len(model.decisions) == 1
        assert bool(model.decisions[0]["conversation"]["recalled"]) is not disable
    finally:
        core.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["outside_excerpt", "delete", "revoke", "source_edit", "neighbor_revoke"])
async def test_recalled_source_changes_before_send_cancel_delivery(pg_store, change):
    sources, keys = seed(pg_store, long=True)
    model, adapter = Model(), Adapter()
    core = Companion(pg_store, model, Host(), [adapter])
    core.start()

    def invalidate():
        recalled = model.decisions[-1]["conversation"]["recalled"]
        target = next(row for row in recalled if row["data"]["id"] == keys[4])
        assert target["excerpt"]["ranges"][0][0] > 0
        assert "周五" in str(recalled)
        if change == "outside_excerpt":
            original = pg_store.read("conversation_turns", keys[4])
            original["data"]["content"] = "更" + original["data"]["content"][1:]
            pg_store.write("conversation_turns", original["data"], original["version"], "fixture", keys[4])
        elif change == "delete":
            with pg_store.db() as db:
                db.execute("DELETE FROM content_records WHERE kind=? AND id=?", ("conversation_turns", keys[4]))
        elif change == "source_edit":
            events.ingest(pg_store, replace(sources[4], text="changed source event"))
        else:
            restrict(pg_store, "conversation_turns/" + keys[5 if change == "neighbor_revoke" else 4])

    adapter.before = invalidate
    try:
        result = await core.handle(event(200, "北岸灯塔"))
        assert result["action"]["status"] == "cancelled" and not adapter.sent
        assert "钴蓝纸鹤" in json.dumps(model.decisions, ensure_ascii=False)
    finally:
        core.stop()
