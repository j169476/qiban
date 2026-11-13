import secrets
from copy import deepcopy

import pytest

from companion_core import events, postgres_transfer
from companion_core.errors import AssistantError
from companion_qq.native_inbox import NativeInbox
from tests.test_qq_native_normalization import IDENTITY, raw
from tests_postgres.test_qq_module import env as env  # noqa: F401


def bound(n):
    return {"account": "10001", "group": "10003", "seq": str(n),
            "senderUin": "10002", "senderUid": "u_peer", "time": "1792922400"}


def message(n, *, mention=False):
    result = raw(n, group=True)
    if mention:
        result["elements"][0]["textElement"].update(atType=2, atUid="10001", atNtUid="u_self")
    return result


def packet(messages, *, page=0, complete=False, lower=1, upper=3, window="range-1", partial=False):
    return {"window_id": window, "kind": "group", "target": "10003", "lower": bound(lower),
            "upper": bound(upper), "page": page, "complete": complete, "messages": messages,
            "history_status": {"status": "incomplete" if partial else "complete",
                               "reasons": ["checkpoint_unavailable"] if partial else []}}


async def test_media_batch_persists_scoped_references_and_replay_cannot_duplicate(env):
    rows = []
    for sequence, number, field in ((2, 2, "picElement"), (3, 4, "pttElement"), (4, 3, "fileElement")):
        row = message(sequence, mention=sequence == 2)
        row["elements"] = (row["elements"] if sequence == 2 else []) + [
            {"elementType": number, "elementId": str(sequence), field: {"filePath": "C:/private/media"}}]
        rows.append(row)
    page = packet(rows, upper=4, complete=True)
    env.module.accept_native_page(page, identity=IDENTITY)
    assert env.module.native_inbox.count() == 3
    replay = deepcopy(page)
    for row in replay["messages"]:
        row["msgId"] = "88888" + row["msgSeq"]
        row["elements"][-1]["elementId"] = "99999"
    env.module.accept_native_page(replay, identity=IDENTITY)
    assert env.module.native_inbox.count() == 3
    pending = await env.module.runtime_poll()
    assert len(pending) == 1
    batch = env.module.decision_batch(pending[0])
    assert len(batch["members"]) == 3
    saved = events.recent(env.store, limit=10)
    attachments = [a for item in saved for a in item["event"]["attachments"]]
    assert {a["kind"] for a in attachments} == {"image", "audio", "file"}
    assert len({a["id"] for a in attachments}) == 3
    assert "C:/private/media" not in str(saved)
    assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    material = env.model.calls[0]["event_material"]
    assert material["images_read"] == material["attachments_read"] == 0
    assert material["attachment_status"] == "not_processed"
    env.module.accept_native_page(replay, identity=IDENTITY)
    assert await env.module.runtime_poll() == []
    assert len(env.provider.sent) == len(env.model.calls) == 1


def test_malformed_media_rolls_back_entire_native_page(env):
    invalid = {**message(3), "elements": [{"elementType": 2, "picElement": None}]}
    with pytest.raises(AssistantError) as error:
        env.module.accept_native_page(packet([message(2), invalid], complete=True), identity=IDENTITY)
    assert error.value.code == "qq_media_invalid"
    assert env.module.native_inbox.state("group", "10003") is None
    assert env.module.native_inbox.count() == 0
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 0


async def test_partial_pages_resume_and_freeze_once_with_history_notice(env):
    first = packet([message(2, mention=True)], partial=True)
    assert env.module.accept_native_page(first, identity=IDENTITY) == {"next_page": 1, "complete": False}
    assert await env.module.runtime_poll() == []
    assert not env.model.calls and not env.provider.sent
    env.module.native_inbox = NativeInbox(env.module)
    assert env.module.native_inbox.state("group", "10003")["next_page"] == 1
    assert env.module.accept_native_page(first, identity=IDENTITY)["next_page"] == 1
    rebound = deepcopy(first)
    rebound["messages"][0]["msgId"] = "987654321"
    assert env.module.accept_native_page(rebound, identity=IDENTITY)["next_page"] == 1
    final = packet([message(3)], page=1, complete=True)
    env.module.accept_native_page(final, identity=IDENTITY)
    pending = await env.module.runtime_poll()
    assert len(pending) == 1
    frozen = env.module.decision_batch(pending[0])
    assert len(frozen["members"]) == 2 and frozen["history_status"] == first["history_status"]
    assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    assert len(env.provider.sent) == len(env.model.calls) == 1
    assert env.model.calls[0]["history_status"]["complete"] is False
    env.module.accept_native_page(final, identity=IDENTITY)
    assert await env.module.runtime_poll() == []
    next_page = packet([message(4, mention=True)], lower=3, upper=4, window="range-2", complete=True)
    env.module.accept_native_page(next_page, identity=IDENTITY)
    newer = (await env.module.runtime_poll())[0]
    assert env.module.decision_batch(newer)["history_status"] == {"status": "complete", "reasons": []}
    assert env.store.state(frozen["key"])["history_status"] == first["history_status"]


async def test_page_failure_rolls_back_messages_queue_and_checkpoint(env, monkeypatch):
    original = events.ingest
    calls = 0

    def fail_second(store, event, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second-row failure")
        return original(store, event, *args, **kwargs)

    page = packet([message(2), message(3)], complete=True)
    with monkeypatch.context() as patch:
        patch.setattr(events, "ingest", fail_second)
        with pytest.raises(RuntimeError, match="second-row"):
            env.module.accept_native_page(page, identity=IDENTITY)
    assert env.module.native_inbox.state("group", "10003") is None
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM qq_native_inbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 0
    env.module.accept_native_page(page, identity=IDENTITY)
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM qq_native_inbox").fetchone()[0] == 2


async def test_incomplete_group_does_not_block_complete_private_window(env):
    env.module.accept_native_page(packet([message(2)], partial=True), identity=IDENTITY)

    def private_bound(n):
        item = raw(n)
        return {"accountUin": "10001", "accountUid": "u_self", "peerUin": "10002", "peerUid": "u_peer",
                "senderUin": "10002", "senderUid": "u_peer", "clientSeq": item["clientSeq"],
                "time": item["msgTime"], "random": item["msgRandom"]}

    private = {"window_id": "private-1", "kind": "private", "target": "10002",
               "lower": private_bound(1), "upper": private_bound(2), "page": 0, "complete": True,
               "messages": [raw(2)], "history_status": {"status": "complete", "reasons": []}}
    env.module.accept_native_page(private, identity=IDENTITY)
    rebound = deepcopy(private)
    rebound["messages"][0].update(msgId="777777", msgSeq="9999")
    env.module.accept_native_page(rebound, identity=IDENTITY)
    pending = await env.module.runtime_poll()
    assert len(pending) == 1 and pending[0].visibility == "private"
    assert env.module.decision_batch(pending[0])["history_status"] == private["history_status"]
    assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    assert len(env.provider.sent) == 1


async def test_reject_conflicting_replay_scope_and_premature_next_window(env):
    first = packet([message(2)], complete=False)
    env.module.accept_native_page(first, identity=IDENTITY)
    changed = deepcopy(first)
    changed["messages"][0]["elements"][0]["textElement"]["content"] = "changed"
    with pytest.raises(AssistantError, match="."):
        env.module.accept_native_page(changed, identity=IDENTITY)
    with pytest.raises(AssistantError):
        env.module.accept_native_page(packet([message(3)], page=2), identity=IDENTITY)
    with pytest.raises(AssistantError):
        env.module.accept_native_page(packet([message(4)], window="range-2", lower=3, upper=4), identity=IDENTITY)
    with pytest.raises(AssistantError):
        env.module.accept_native_page(packet([raw(3)], page=1, complete=True), identity=IDENTITY)
    with pytest.raises(AssistantError):
        env.module.accept_native_page(first, identity={**IDENTITY, "uid": "u_different"})
    env.module.accept_native_page(packet([message(3)], page=1, complete=True), identity=IDENTITY)
    with pytest.raises(AssistantError) as error:
        env.module.accept_native_page(packet([message(4)], window="range-2", lower=3, upper=4), identity=IDENTITY)
    assert error.value.code == "qq_inbox_backpressure"


async def test_2049_native_messages_use_database_queue_without_early_reply(env):
    for page, start in enumerate(range(2, 2051, 100)):
        messages = [message(n, mention=n == 2) for n in range(start, min(start + 100, 2051))]
        complete = start + 100 >= 2051
        env.module.accept_native_page(packet(messages, page=page, complete=complete, upper=2050), identity=IDENTITY)
        pending = await env.module.runtime_poll()
        if not complete:
            assert pending == []
        assert not env.model.calls and not env.provider.sent
    assert len(pending) == 1
    assert len(env.module.decision_batch(pending[0])["members"]) == 2049
    assert env.store.state(env.module.queue_key, []) == []
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM qq_native_inbox").fetchone()[0] == 2049
        assert all(row[0].startswith("enc1:") for row in db.execute("SELECT body FROM core_events"))


async def test_backup_includes_pending_native_queue_and_incomplete_position(env):
    env.module.accept_native_page(packet([message(2)]), identity=IDENTITY)
    env.core.stop()
    key = secrets.token_bytes(32)
    backup = postgres_transfer.create(env.store, key)
    snapshot = postgres_transfer.unpack(backup, key)
    queue = snapshot["tables"]["qq_native_inbox"]
    assert len(queue["rows"]) == 1
    env.module.accept_native_page(packet([message(3)], page=1, complete=True), identity=IDENTITY)
    assert env.module.native_inbox.state("group", "10003")["complete"] is True
    postgres_transfer.restore(env.store, backup, key, expected=postgres_transfer.fingerprint(env.store))
    assert env.module.native_inbox.state("group", "10003")["complete"] is False
    assert env.module.native_inbox.state("group", "10003")["next_page"] == 1
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM qq_native_inbox").fetchone()[0] == 1
