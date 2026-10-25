"""Durable per-feed notification pagination with fake SDK reads only."""
import json
from types import SimpleNamespace

import pytest

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError
from bili_assistant.storage import Store


def feed(monkeypatch, timestamp, *, source="replies", pages=None, authors=None):
    from bilibili_api import session

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args: object())
    time_key = "reply_time" if source == "replies" else "at_time"
    id_key = "last_reply_id" if source == "replies" else "last_id"
    calls = []
    pages = pages if pages is not None else {
        None: (list(range(21, 1, -1)), {"id": 2, "time": timestamp, "is_end": False}),
        2: ([1], {"id": 1, "time": timestamp, "is_end": True}),
    }

    async def read(_credential, **params):
        calls.append(params)
        ids, cursor = pages[params.get(id_key)]
        return {"items": [{"id": number, time_key: timestamp, "user": {"mid": (authors or {}).get(number, 789)}, "item": {
            "type": "reply", "business_id": 1, "subject_id": 999,
            "root_id": number, "source_id": number, "source_content": f"reply {number}",
        }} for number in ids], "cursor": cursor}

    async def empty(*args, **kwargs):
        return {}

    monkeypatch.setattr(session, "get_replies", read if source == "replies" else empty)
    monkeypatch.setattr(session, "get_at", read if source == "mentions" else empty)
    monkeypatch.setattr(session, "get_sessions", empty)
    return calls, pages


def commit(store, events, account="123"):
    with store.db() as db:
        for event in events:
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)", (
                event["id"], account, event["uid"], event["kind"], event["received"], json.dumps(event)))


@pytest.mark.asyncio
@pytest.mark.parametrize("source,id_key,time_key", [
    ("replies", "last_reply_id", "reply_time"), ("mentions", "last_id", "at_time"),
])
async def test_twenty_plus_one_notifications_progress_across_polls_and_restart(
        role, monkeypatch, source, id_key, time_key):
    timestamp = int(role.clock().timestamp()) + 10
    calls, _ = feed(monkeypatch, timestamp, source=source)
    counts = []
    for _ in range(3):
        role.adapter = Bilibili(Store(role.store.root))
        counts.append((await role.inbox(archive=False, report=True))["new_count"])
    assert counts == [20, 1, 0]
    assert calls == [{}, {id_key: 2, time_key: timestamp}, {}]
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 21


@pytest.mark.asyncio
async def test_uncommitted_page_replays_exact_payload_before_cursor_can_advance(role, monkeypatch):
    timestamp = int(role.clock().timestamp()) + 10
    calls, pages = feed(monkeypatch, timestamp)
    started = role.run()["started_at"]
    first = await Bilibili(role.store).inbox(started)
    assert len(first) == 20 and calls == [{}]
    # The remote head changes while the combined inbox transaction never committed.
    pages[None] = ([99], {"id": 99, "time": timestamp + 1, "is_end": True})
    restarted = Bilibili(Store(role.store.root))
    assert await restarted.inbox(started) == first
    commit(role.store, first[:-1])
    assert await restarted.inbox(started) == first
    assert calls == [{}]
    role.adapter = restarted
    assert (await role.inbox(archive=False, report=True))["new_count"] == 1
    assert (await role.inbox(archive=False, report=True))["new_count"] == 1
    assert calls[-1] == {"last_reply_id": 2, "reply_time": timestamp}
    assert (await role.inbox(archive=False, report=True))["new_count"] == 1
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 22


@pytest.mark.asyncio
async def test_committed_duplicate_boundary_does_not_starve_later_pages(role, monkeypatch):
    timestamp = int(role.clock().timestamp()) + 10
    pages = {
        None: ([5, 4], {"id": 4, "time": timestamp, "is_end": False}),
        4: ([4, 3, 2], {"id": 2, "time": timestamp, "is_end": False}),
        2: ([2, 1], {"id": 1, "time": timestamp, "is_end": True}),
    }
    calls, _ = feed(monkeypatch, timestamp, pages=pages)
    counts = []
    for _ in range(4):
        role.adapter = Bilibili(role.store)
        counts.append((await role.inbox(archive=False, report=True))["new_count"])
    assert counts == [2, 2, 1, 0]
    assert [call.get("last_reply_id") for call in calls] == [None, 4, 2, None]


@pytest.mark.asyncio
async def test_notification_pending_page_is_scoped_to_account_and_run(role, monkeypatch):
    timestamp = int(role.clock().timestamp()) + 10
    calls, _ = feed(monkeypatch, timestamp)
    started = role.run()["started_at"]
    adapter = Bilibili(role.store)
    first = await adapter.inbox(started)
    commit(role.store, first)
    assert len(await adapter.inbox(started)) == 1
    assert calls[-1].get("last_reply_id") == 2
    restarted_at = role.clock().replace(second=1).isoformat()
    assert len(await adapter.inbox(restarted_at)) == 20
    assert calls[-1] == {}
    manifest = role.store.read("manifest")
    manifest["data"]["account_uid"] = "456"
    role.store.write("manifest", manifest["data"], manifest["version"], "test")
    assert len(await adapter.inbox(started)) == 20
    assert calls[-1] == {}
    assert len(await adapter.inbox(started)) == 20  # 123's rows cannot acknowledge 456's batch.
    assert len(calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [
    {"id": 2, "time": 1792476010, "is_end": False},
    {"id": 3, "time": 1792476010, "is_end": False},
    {"id": 1, "time": 1, "is_end": False},
])
async def test_nonadvancing_or_pre_run_cursor_returns_to_head(role, monkeypatch, cursor):
    timestamp = int(role.clock().timestamp()) + 10
    pages = {None: ([3, 2], {"id": 2, "time": timestamp, "is_end": False}), 2: ([1], cursor)}
    calls, _ = feed(monkeypatch, timestamp, pages=pages)
    role.adapter = Bilibili(role.store)
    for _ in range(3):
        await role.inbox(archive=False)
    assert [call.get("last_reply_id") for call in calls] == [None, 2, None]


@pytest.mark.asyncio
async def test_later_feed_failure_cannot_lose_already_read_page(role, monkeypatch):
    from bilibili_api import session

    timestamp = int(role.clock().timestamp()) + 10
    calls, _ = feed(monkeypatch, timestamp)
    fail = True

    async def mentions(*args, **kwargs):
        if fail:
            raise AssistantError("platform_read_error", "simulated sibling feed failure")
        return {}

    monkeypatch.setattr(session, "get_at", mentions)
    role.adapter = Bilibili(role.store)
    with pytest.raises(AssistantError):
        await role.inbox(archive=False)
    fail = False
    assert (await role.inbox(archive=False, report=True))["new_count"] == 20
    assert calls == [{}]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_pending", [False, True])
async def test_own_notifications_excluded_by_engine_never_hold_page_acknowledgement(role, monkeypatch, legacy_pending):
    timestamp = int(role.clock().timestamp()) + 10
    calls, _ = feed(monkeypatch, timestamp, authors={number: 123 for number in range(2, 22)})
    role.adapter = Bilibili(role.store)
    assert (await role.inbox(archive=False, report=True))["new_count"] == 0
    key = "inbox-notification-page:123:replies"
    if legacy_pending:
        state = role.store.state(key)
        state["pending"]["events"].append({"id": "self-not-persisted", "uid": "123"})
        role.store.set_state(key, state)
    assert (await role.inbox(archive=False, report=True))["new_count"] == 1
    assert [call.get("last_reply_id") for call in calls] == [None, 2]


def private_feed(monkeypatch, timestamp, uids=(789,)):
    from bilibili_api import Credential, session
    from bilibili_api.utils import network

    import bili_assistant.credentials as credentials

    credential = Credential(sessdata="fake", bili_jct="fake", buvid3="fake3", buvid4="fake4")
    monkeypatch.setattr(credentials, "load", lambda *args: credential)
    visible, calls, pages = list(uids), [], {}
    for uid in uids:
        for sequence, ids in ((0, range(31, 1, -1)), (2, [1])):
            pages[uid, sequence] = {"has_more": sequence == 0, "messages": [{
                "timestamp": timestamp, "sender_uid": uid, "msg_seqno": number,
                "msg_type": 1, "content": {"content": f"message {number}"},
            } for number in ids]}

    async def empty(*args, **kwargs):
        return {}

    async def sessions(*args, **kwargs):
        return {"session_list": [{"talker_id": uid, "session_ts": timestamp * 1_000_000} for uid in visible]}

    async def ticket(_credential):
        return "fake-ticket", "9999999999"

    async def wbi(_credential):
        return "0123456789abcdef0123456789abcdef"

    class Client:
        async def request(self, **request):
            assert request["method"] == "GET"
            assert request["url"] == "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
            params = request["params"]
            assert params["size"] == 30 and params["session_type"] == 1 and params["begin_seqno"] == 0
            uid, end = params["talker_id"], params.get("end_seqno", 0)
            calls.append((uid, end))
            page = pages[uid, end]
            return SimpleNamespace(code=200, headers={}, utf8_text=lambda: json.dumps({"code": 0, "data": page}))

    monkeypatch.setattr(session, "get_replies", empty)
    monkeypatch.setattr(session, "get_at", empty)
    monkeypatch.setattr(session, "get_sessions", sessions)
    monkeypatch.setattr(network, "get_bili_ticket", ticket)
    monkeypatch.setattr(network, "get_wbi_mixin_key", wbi)
    monkeypatch.setattr(network, "get_client", lambda: Client())
    return calls, pages, visible


@pytest.mark.asyncio
async def test_thirty_plus_one_private_messages_progress_across_polls_and_restart(role, monkeypatch):
    calls, _, _ = private_feed(monkeypatch, int(role.clock().timestamp()) + 10)
    counts = []
    for _ in range(3):
        role.adapter = Bilibili(Store(role.store.root))
        counts.append((await role.inbox(archive=False, report=True))["new_count"])
    assert counts == [30, 1, 0]
    assert calls == [(789, 0), (789, 2), (789, 0)]


@pytest.mark.asyncio
async def test_private_pending_page_and_backlog_remain_queued_when_session_disappears(role, monkeypatch):
    calls, pages, visible = private_feed(monkeypatch, int(role.clock().timestamp()) + 10)
    started = role.run()["started_at"]
    first = await Bilibili(role.store).inbox(started)
    assert len(first) == 30
    visible.clear()
    pages[789, 0]["messages"] = []
    role.adapter = Bilibili(Store(role.store.root))
    assert await role.adapter.inbox(started) == first
    commit(role.store, first[:-1])
    assert (await role.inbox(archive=False, report=True))["new_count"] == 1
    assert calls == [(789, 0)]
    assert (await role.inbox(archive=False, report=True))["new_count"] == 1
    assert (await role.inbox(archive=False, report=True))["new_count"] == 0
    assert calls == [(789, 0), (789, 2)]


@pytest.mark.asyncio
async def test_private_pagination_stays_fair_and_bounded_across_twenty_five_sessions(role, monkeypatch):
    uids = tuple(range(700, 725))
    calls, _, _ = private_feed(monkeypatch, int(role.clock().timestamp()) + 10, uids)
    for _ in range(5):
        before = len(calls)
        role.adapter = Bilibili(Store(role.store.root))
        await role.inbox(archive=False)
        assert len(calls) - before <= 10
    assert {uid for uid, sequence in calls if sequence == 2} == set(uids)
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 25 * 31


@pytest.mark.asyncio
async def test_private_pending_page_is_scoped_to_account_and_run(role, monkeypatch):
    calls, _, _ = private_feed(monkeypatch, int(role.clock().timestamp()) + 10)
    started = role.run()["started_at"]
    adapter = Bilibili(role.store)
    first = await adapter.inbox(started)
    commit(role.store, first)
    assert len(await adapter.inbox(started)) == 1
    assert calls[-1] == (789, 2)
    assert len(await adapter.inbox(role.clock().replace(second=1).isoformat())) == 30
    assert calls[-1] == (789, 0)
    manifest = role.store.read("manifest")
    manifest["data"]["account_uid"] = "456"
    role.store.write("manifest", manifest["data"], manifest["version"], "test")
    assert len(await adapter.inbox(started)) == 30
    assert len(await adapter.inbox(started)) == 30
    assert calls == [(789, 0), (789, 2), (789, 0), (789, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", ["own", "unsupported"])
async def test_private_page_with_no_supported_incoming_messages_can_advance(role, monkeypatch, excluded):
    calls, pages, _ = private_feed(monkeypatch, int(role.clock().timestamp()) + 10)
    for message in pages[789, 0]["messages"]:
        message["sender_uid" if excluded == "own" else "msg_type"] = 123 if excluded == "own" else 999
    role.adapter = Bilibili(role.store)
    assert (await role.inbox(archive=False, report=True))["new_count"] == 0
    assert (await role.inbox(archive=False, report=True))["new_count"] == 1
    assert calls == [(789, 0), (789, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_since", "repeated_sequence"])
async def test_private_page_stops_at_run_start_or_nonadvancing_sequence(role, monkeypatch, boundary):
    calls, pages, _ = private_feed(monkeypatch, int(role.clock().timestamp()) + 10)
    if boundary == "before_since":
        pages[789, 0]["messages"][-1]["timestamp"] = int(role.clock().timestamp()) - 1
        expected = [(789, 0), (789, 0), (789, 0)]
    else:
        pages[789, 2] = {"has_more": True, "messages": [pages[789, 0]["messages"][-1]]}
        expected = [(789, 0), (789, 2), (789, 0)]
    role.adapter = Bilibili(role.store)
    for _ in range(3):
        await role.inbox(archive=False)
    assert calls == expected
