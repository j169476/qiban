"""Session-list discovery through real SDK preparation and fake HTTP responses."""
import json
from types import SimpleNamespace

import pytest

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError
from bili_assistant.storage import Store


def session_server(monkeypatch, timestamp, count=21):
    from bilibili_api import Credential, session
    from bilibili_api.utils import network

    import bili_assistant.credentials as credentials

    credential = Credential(sessdata="fake", bili_jct="fake", buvid3="fake3", buvid4="fake4")
    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: credential)
    original = [{"talker_id": 700 + offset, "session_type": 1,
                 "session_ts": (timestamp - offset) * 1_000_000}
                for offset in range(count)]
    server = {"visible": list(original), "session_calls": [], "message_calls": [],
              "fail_message": False, "ignore_cursor": False, "force_more": None}

    async def empty(*args, **kwargs):
        return {}

    async def ticket(_credential):
        return "fake-ticket", "9999999999"

    async def wbi(_credential):
        return "0123456789abcdef0123456789abcdef"

    class Client:
        async def request(self, **request):
            assert request["method"] == "GET"
            params = request["params"]
            assert params["session_type"] == 1
            if request["url"] == "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions":
                assert params["group_fold"] == 1 and params["unfollow_fold"] == 0
                assert params["sort_rule"] == 2 and params["mobi_app"] == "web" and params["build"] == 0
                limit, end = params.get("size", 20), params.get("end_ts", 0)
                assert limit == 20
                server["session_calls"].append(end)
                eligible = sorted((row for row in server["visible"]
                                   if not end or server["ignore_cursor"] or row["session_ts"] < end),
                                  key=lambda row: row["session_ts"], reverse=True)
                more = len(eligible) > limit if server["force_more"] is None else server["force_more"]
                data = {"session_list": eligible[:limit], "has_more": more}
            else:
                assert request["url"] == "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
                assert params["size"] == 30 and params["begin_seqno"] == 0
                uid = params["talker_id"]
                server["message_calls"].append(uid)
                if server["fail_message"]:
                    raise AssistantError("platform_read_error", "simulated message read failure")
                data = {"has_more": False, "messages": [{"timestamp": timestamp, "sender_uid": uid,
                        "msg_seqno": 1, "msg_type": 1, "content": {"content": f"message from {uid}"}}]}
            return SimpleNamespace(code=200, headers={}, utf8_text=lambda: json.dumps({"code": 0, "data": data}))

    monkeypatch.setattr(session, "get_replies", empty)
    monkeypatch.setattr(session, "get_at", empty)
    monkeypatch.setattr(network, "get_bili_ticket", ticket)
    monkeypatch.setattr(network, "get_wbi_mixin_key", wbi)
    monkeypatch.setattr(network, "get_client", lambda: Client())
    return server, original


async def poll(role, server):
    before = len(server["message_calls"]), len(server["session_calls"])
    role.adapter = Bilibili(Store(role.store.root))
    result = await role.inbox(archive=False, report=True)
    assert len(server["message_calls"]) - before[0] <= 10
    assert len(server["session_calls"]) - before[1] <= 1
    return result


def received_uids(store):
    with store.db() as db:
        return {int(row["uid"]) for row in db.execute("SELECT uid FROM events")}


@pytest.mark.asyncio
@pytest.mark.parametrize("head_disappears", [False, True])
async def test_twenty_plus_one_sessions_page_and_keep_unscheduled_uids_after_restart(role, monkeypatch, head_disappears):
    server, original = session_server(monkeypatch, int(role.clock().timestamp()) + 100)
    for number in range(6):
        await poll(role, server)
        if number == 0 and head_disappears:
            server["visible"] = original[20:]
    assert received_uids(role.store) == set(range(700, 721))
    assert server["session_calls"][:3] == [0, original[19]["session_ts"], 0]


@pytest.mark.asyncio
async def test_discovery_queue_survives_failure_before_first_message_page(role, monkeypatch):
    server, original = session_server(monkeypatch, int(role.clock().timestamp()) + 100)
    server["fail_message"] = True
    with pytest.raises(AssistantError):
        await poll(role, server)
    saved = role.store.state("inbox-session-page:123")
    assert saved["uids"] == list(range(700, 720)) and saved["next_cursor"] == original[19]["session_ts"]
    server["fail_message"] = False
    server["visible"] = original[20:]
    for _ in range(4):
        await poll(role, server)
    assert received_uids(role.store) == set(range(700, 721))


@pytest.mark.asyncio
async def test_new_head_is_discovered_after_history_sweep(role, monkeypatch):
    timestamp = int(role.clock().timestamp()) + 100
    server, _ = session_server(monkeypatch, timestamp)
    await poll(role, server)
    server["visible"].append({"talker_id": 999, "session_type": 1, "session_ts": (timestamp + 1) * 1_000_000})
    for _ in range(4):
        await poll(role, server)
    assert received_uids(role.store) == set(range(700, 721)) | {999}
    assert server["session_calls"][2] == 0


@pytest.mark.asyncio
async def test_discovery_backpressure_never_drops_or_advances_queued_work(role, monkeypatch):
    server, _ = session_server(monkeypatch, int(role.clock().timestamp()) + 100, count=81)
    for _ in range(4):
        await poll(role, server)
    saved = role.store.state("inbox-session-page:123")
    assert len(saved["uids"]) == 40
    count = len(server["session_calls"])
    await poll(role, server)
    current = role.store.state("inbox-session-page:123")
    assert len(server["session_calls"]) == count and current["next_cursor"] == saved["next_cursor"]
    assert len(current["uids"]) == 30
    for _ in range(7):
        await poll(role, server)
        assert len(role.store.state("inbox-session-page:123")["uids"]) < 60
    assert received_uids(role.store) == set(range(700, 781))


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["run", "account"])
async def test_list_cursor_and_unscheduled_queue_are_scoped_to_run_and_account(role, monkeypatch, boundary):
    server, _ = session_server(monkeypatch, int(role.clock().timestamp()) + 100)
    started = role.run()["started_at"]
    first = await Bilibili(role.store).inbox(started)  # Deliberately do not commit its messages.
    assert len(first) == 10 and role.store.state("inbox-session-page:123")["uids"] == list(range(710, 720))
    server["visible"] = []
    if boundary == "run":
        started = role.clock().replace(second=1).isoformat()
    else:
        manifest = role.store.read("manifest")
        manifest["data"]["account_uid"] = "456"
        role.store.write("manifest", manifest["data"], manifest["version"], "test")
    assert await Bilibili(Store(role.store.root)).inbox(started) == []
    assert server["session_calls"] == [0, 0] and len(server["message_calls"]) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["since", "repeated", "forward"])
async def test_list_paging_stops_at_run_start_and_nonadvancing_cursor(role, monkeypatch, boundary):
    timestamp = int(role.clock().timestamp()) + (10 if boundary == "since" else 100)
    server, original = session_server(monkeypatch, timestamp)
    await poll(role, server)
    if boundary == "since":
        expected = [0, 0, 0]
    else:
        server["ignore_cursor"] = True
        if boundary == "forward":
            server["visible"] = [{**row, "session_ts": row["session_ts"] + 1_000_000} for row in original]
        expected = [0, original[19]["session_ts"], 0]
    for _ in range(2):
        await poll(role, server)
    assert server["session_calls"] == expected
    if boundary == "since":
        assert received_uids(role.store) == set(range(700, 711))
