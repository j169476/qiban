import json
import sqlite3
from copy import deepcopy
from datetime import timedelta

import pytest
from conftest import FakeHost

from bili_assistant.api import call
from bili_assistant.cognition import packet
from bili_assistant.conversations import (
    append,
    context,
    record_event,
    recover_platform_turns,
    sync_codex,
    thread_id,
)
from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError
from bili_assistant.storage import Store


def event(role, item_id="dm-789-1", uid="789", **extra):
    return {"id": item_id, "kind": "dm_reply", "uid": uid, "text": "前文里的具体约定",
            "received": role.clock().isoformat(), **extra}


async def receive(role, *items):
    async def inbox(since):
        return list(items)
    role.adapter.inbox = inbox
    return await role.inbox()


@pytest.mark.asyncio
async def test_incoming_outgoing_restart_and_deduplication(role):
    incoming = event(role)
    await receive(role, incoming)
    result = await role.execute(role.prepare("dm_reply", {"event_id": incoming["id"], "text": "已经记下"})["id"])
    assert result["status"] == "succeeded"
    await receive(role, incoming)
    reopened = Engine(Store(role.store.root), FakeHost(), role.adapter, role.clock)
    recover_platform_turns(reopened)
    history = reopened.conversation_context(incoming["id"])
    assert [r["data"]["content"] for r in history["turns"]] == ["前文里的具体约定", "已经记下"]
    assert [r["data"]["status"] for r in history["turns"]] == ["received", "sent"]
    supplied, _ = packet(reopened, {"events": [incoming]})
    assert supplied["conversation_threads"][0]["total_turns"] == 2
    assert not reopened.store.context()["records"]  # Raw DM is not global recall.
    assert not reopened.store.search("前文")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, expected", [("transport", "uncertain"), ("rejected", "failed")])
async def test_failure_never_becomes_a_successful_reply(role, failure, expected):
    incoming = event(role)
    await receive(role, incoming)
    async def fail(*args):
        if failure == "transport":
            raise TimeoutError()
        raise AssistantError("platform_rejected", "rejected")
    role.adapter.perform = fail
    action = role.prepare("dm_reply", {"event_id": incoming["id"], "text": "发送尝试"})
    await role.execute(action["id"])
    turns = role.conversation_context(incoming["id"])["turns"]
    assert turns[-1]["data"]["status"] == expected
    assert not (await role.execute(action["id"]))["sent"]


@pytest.mark.asyncio
async def test_comment_roots_senders_and_accounts_are_scoped(role):
    first = event(role, "reply-1", kind="reply", oid=10, root=11, parent=12)
    same_root = event(role, "reply-2", uid="780", kind="reply", oid=10, root=11, parent=13)
    other = event(role, "reply-3", kind="reply", oid=10, root=22, parent=23)
    dm = event(role, "dm-780-1", uid="780")
    await receive(role, first, same_root, other, dm)
    assert len(role.conversation_context(first["id"])["turns"]) == 2
    assert len(role.conversation_context(other["id"])["turns"]) == 1
    assert len(role.conversation_context(dm["id"])["turns"]) == 1
    shared_tid = role.conversation_context(first["id"])["thread_id"]
    assert context(role.store, "999", shared_tid)["turns"] == []
    payload, _ = packet(role, {"events": [dm]})
    assert [r["data"]["content"] for r in payload["conversation_threads"][0]["turns"]] == [dm["text"]]


@pytest.mark.asyncio
async def test_edits_and_deletions_survive_reimport(role):
    item = event(role)
    await receive(role, item)
    record = role.conversation_context(item["id"])["turns"][0]
    doc = record["data"]
    doc.update(content="人工纠正的版本", locked=True)
    role.store.write("conversation_turns", doc, record["version"], "user", doc["id"])
    await receive(role, item)
    assert role.conversation_context(item["id"])["turns"][0]["data"]["content"] == "人工纠正的版本"
    role.store.path("conversation_turns", doc["id"]).unlink()
    await receive(role, item)
    assert role.conversation_context(item["id"])["turns"] == []


def test_archive_write_recovers_after_crash_before_receipt(role, monkeypatch):
    item = event(role)
    original = role.store.set_state
    def crash(key, value, db=None):
        if key.startswith("conversation-recorded:"):
            raise OSError("interrupted")
        return original(key, value, db)
    monkeypatch.setattr(role.store, "set_state", crash)
    with pytest.raises(OSError):
        record_event(role.store, "123", item)
    monkeypatch.setattr(role.store, "set_state", original)
    record_event(role.store, "123", item)
    assert len(role.store.records("conversation_turns")[0]) == 1


def test_history_bound_and_versions(role):
    settings = role.store.read("companion")
    settings["data"].update(conversation_context_turns=4, conversation_context_chars=3000)
    role.store.write("companion", settings["data"], settings["version"], "test")
    start = role.clock()
    for i in range(12):
        item = event(role, f"dm-789-{i}", received=(start + timedelta(seconds=i)).isoformat())
        record_event(role.store, "123", item)
    result = context(role.store, "123", thread_id("123", "dm", "789"))
    assert 0 < len(result["turns"]) <= 4
    assert result["truncated"] and result["context_chars"] <= 3000
    assert result["turns"][-1]["data"]["message_id"] == "dm-789-11"
    payload, versions = packet(role, {"events": [item]})
    assert any(kind == "conversation_turns" for kind, _, _ in versions)
    assert payload["conversation_threads"][0]["context_chars"] <= 3000


def turn(item_id, status="completed"):
    return {"id": item_id, "status": status, "itemsView": "full",
            "startedAt": 1792480000, "completedAt": 1792480001,
            "items": [
                {"id": item_id + "-user", "type": "userMessage", "content": [{"type": "text", "text": "聊聊这个"}]},
                {"id": item_id + "-reason", "type": "reasoning", "text": "never import"},
                {"id": item_id + "-progress", "type": "agentMessage", "phase": "commentary", "text": "progress"},
                {"id": item_id + "-answer", "type": "agentMessage", "phase": "final_answer", "text": "正式回答"},
            ]}


def test_host_sync_current_user_only_then_completed_answer(role, monkeypatch):
    data = [turn("one", "inProgress")]
    monkeypatch.setattr(role.host, "conversation_page", lambda cursor: {"data": data}, raising=False)
    role.stop(paused=True)
    assert sync_codex(role)["imported"] == 1
    assert not sync_codex(role)["caught_up"]
    assert role.conversation_context()["turns"][0]["data"]["speaker"] == "user"
    data[0]["status"] = "completed"
    assert sync_codex(role)["imported"] == 1
    assert sync_codex(role)["imported"] == 0
    assert len(role.conversation_context()["turns"]) == 2
    assert role.store.state("run")["status"] == "paused"


def test_host_sync_pagination_continues_without_dropping_last_page(role, monkeypatch):
    pages = {None: {"data": [turn("one")], "nextCursor": "p2"},
             "p2": {"data": [turn("two")], "nextCursor": "p3"},
             "p3": {"data": [turn("three")], "nextCursor": "p4"},
             "p4": {"data": [turn("four")]}}
    monkeypatch.setattr(role.host, "conversation_page", lambda cursor: deepcopy(pages[cursor]), raising=False)
    assert not sync_codex(role)["caught_up"]
    assert sync_codex(role)["caught_up"]
    pages["p4"]["data"].append(turn("five"))
    assert sync_codex(role)["imported"] == 2
    assert role.conversation_context()["total_turns"] == 10
    assert [r["data"]["message_id"] for r in role.conversation_context()["turns"]][::2] == [
        k + "-user" for k in ("one", "two", "three", "four", "five")]


@pytest.mark.asyncio
async def test_admin_and_unknown_event_cannot_access_role_transcript(role, admin):
    with pytest.raises(AssistantError):
        await call(admin, "conversation_sync", {})
    with pytest.raises(AssistantError):
        await call(admin, "conversation_context", {})
    with pytest.raises(AssistantError):
        role.conversation_context("invented")
    with pytest.raises(AssistantError):
        await call(role, "memory_get", {"kind": "conversation_turns", "id": "invented"})


def test_long_host_text_preserved_in_parts(role):
    content = "保持原文" * 8000
    append(role.store, "123", "codex", "role", "long-user-message", content, "user", "",
           "received", role.clock().isoformat())
    items = role.store.records("conversation_turns")[0]
    assert "".join(r["data"]["content"] for r in sorted(items, key=lambda r: r["data"]["part"])) == content
    result = role.conversation_context()
    assert result["turns"] and result["context_chars"] <= 12000


def test_legacy_event_database_upgrade_retains_rows(tmp_path):
    root = tmp_path / "legacy"
    (root / "runtime").mkdir(parents=True)
    with sqlite3.connect(root / "runtime" / "state.sqlite3") as db:
        db.execute("CREATE TABLE events(id TEXT PRIMARY KEY,account TEXT,uid TEXT,kind TEXT,received TEXT,body TEXT)")
        db.execute("INSERT INTO events VALUES ('same','123','789','reply','2026-10-21','{}')")
    upgraded = Store(root)
    with upgraded.db() as db:
        assert db.execute("SELECT account FROM events").fetchone()[0] == "123"
        db.execute("INSERT INTO events VALUES ('same','456','789','reply','2026-10-21','{}')")
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_same_external_event_id_in_two_accounts(role):
    item = event(role)
    await receive(role, item)
    first = role.conversation_context(item["id"])
    manifest = role.store.read("manifest")
    manifest["data"]["account_uid"] = "456"
    role.store.write("manifest", manifest["data"], manifest["version"], "test")
    role.start()
    item["text"] = "属于另一个账号的上下文"
    await receive(role, item)
    second = role.conversation_context(item["id"])
    assert first["thread_id"] != second["thread_id"]
    assert second["turns"][0]["data"]["content"] == item["text"]


@pytest.mark.asyncio
async def test_completed_action_archive_can_recover_without_resending(role, monkeypatch):
    item = event(role)
    await receive(role, item)
    import bili_assistant.conversations as module
    original = module.record_action
    monkeypatch.setattr(module, "record_action", lambda *args: (_ for _ in ()).throw(OSError("disk failed")))
    action = role.prepare("dm_reply", {"event_id": item["id"], "text": "已送达"})
    with pytest.raises(OSError):
        await role.execute(action["id"])
    monkeypatch.setattr(module, "record_action", original)
    recover_platform_turns(role)
    assert len(role.adapter.sent) == 1
    assert role.conversation_context(item["id"])["turns"][-1]["data"]["status"] == "sent"
    with role.store.db() as db:
        assert json.loads(db.execute("SELECT result FROM actions WHERE id=?", (action["id"],)).fetchone()[0])
