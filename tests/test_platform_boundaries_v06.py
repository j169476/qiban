"""Platform dispatch and ingestion regressions; real SDK preparation, fake I/O."""

import asyncio
import json

import pytest

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError

CASES = [
    ("comment", {"bvid": "BV1xx411c7mD", "text": "评论"}),
    ("reply", {"text": "回复", "_event": {"oid": 999, "root": 111, "parent": 222}}),
    ("reply", {"text": "@测试 回复", "mentions": {"测试": "789"},
               "_event": {"oid": 999, "root": 111, "parent": 222}}),
    ("dm_reply", {"text": "私信", "_event": {"uid": "789"}}),
    ("comment_like", {"_event": {"oid": 999, "parent": 222}}),
    ("dynamic", {"text": "动态"}),
    ("like", {"bvid": "BV1xx411c7mD"}),
    ("coin", {"bvid": "BV1xx411c7mD", "units": 1}),
    ("favorite", {"bvid": "BV1xx411c7mD"}),
    ("follow", {"uid": "789"}),
]


@pytest.fixture
def sdk(monkeypatch):
    from bilibili_api import Credential, user, video
    from bilibili_api.utils import network

    import bili_assistant.credentials as credentials

    credential = Credential(sessdata="fake", bili_jct="fake", buvid3="fake3", buvid4="fake4", dedeuserid="123")
    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: credential)
    sent = []

    async def ticket(_credential):
        return "fake-ticket", "9999999999"

    async def wbi(_credential):
        await asyncio.sleep(0)
        return "0123456789abcdef0123456789abcdef"

    async def liked(_video):
        return bool(sent)

    async def coins(_video):
        return len(sent)

    async def favoured(_video):
        return False

    async def relation(_user):
        return {"be_relation": {"attribute": 0}}

    class Client:
        async def request(self, **kwargs):
            assert kwargs["method"] == "POST"
            sent.append(kwargs)
            return object()

    monkeypatch.setattr(network, "get_bili_ticket", ticket)
    monkeypatch.setattr(network, "get_wbi_mixin_key", wbi)
    monkeypatch.setattr(network, "get_client", lambda: Client())
    monkeypatch.setattr(network.Api, "_process_response", lambda *args, **kwargs: {"rpid": "777"})
    monkeypatch.setattr(video.Video, "has_liked", liked)
    monkeypatch.setattr(video.Video, "get_pay_coins", coins)
    monkeypatch.setattr(video.Video, "has_favoured", favoured)
    monkeypatch.setattr(user.User, "get_relation", relation)
    return network, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,payload", CASES)
async def test_every_write_checks_guard_after_sdk_preparation(store, monkeypatch, sdk, kind, payload):
    network, sent = sdk
    original = network.Api._prepare_request
    trace = []

    async def prepare(api):
        result = await original(api)
        trace.append("preparation_complete")
        return result

    def stopped():
        assert trace == ["preparation_complete"]
        trace.append("guard")
        raise AssistantError("before_send_cancelled", "paused during request preparation")

    monkeypatch.setattr(network.Api, "_prepare_request", prepare)
    with pytest.raises(AssistantError) as error:
        await Bilibili(store).perform(kind, payload, {"favorite_folder_id": 12345}, stopped)
    assert error.value.code == "before_send_cancelled"
    assert trace == ["preparation_complete", "guard"]
    assert not sent


@pytest.mark.asyncio
async def test_pause_during_real_sdk_wbi_preparation_stops_comment(role, monkeypatch, sdk):
    network, sent = sdk
    stop = role.store.runtime / f"stop-{role.run()['generation']}"

    async def wbi(_credential):
        await asyncio.sleep(0)
        stop.write_text("pause", encoding="utf-8")
        return "0123456789abcdef0123456789abcdef"

    monkeypatch.setattr(network, "get_wbi_mixin_key", wbi)
    with pytest.raises(AssistantError):
        await Bilibili(role.store).perform("reply", CASES[1][1], {}, role.run)
    assert stop.exists()
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "archive", "policy"])
async def test_dispatch_ledger_cancels_after_preparation_invalidates_guard(role, monkeypatch, sdk, change):
    network, sent = sdk
    adapter = Bilibili(role.store)

    async def me():
        return {"uid": "123"}

    monkeypatch.setattr(adapter, "me", me)
    role.adapter = adapter
    event = {"id": "reply-guard", "kind": "reply", "uid": "789", "oid": 999,
             "root": 111, "parent": 222, "received": role.clock().isoformat(), "text": "测试"}
    with role.store.db() as db:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (
            event["id"], "123", "789", "reply", event["received"], json.dumps(event)))
    original = network.Api._prepare_request

    async def prepare(api):
        result = await original(api)
        if change == "pause":
            (role.store.runtime / f"stop-{role.run()['generation']}").write_text("pause", encoding="utf-8")
        elif change == "archive":
            role.host.states[role.identity()] = True
        else:
            policy = role.store.read("policy")
            policy["data"]["actions"]["reply"]["enabled"] = False
            role.store.write("policy", policy["data"], policy["version"], "test")
        return result

    monkeypatch.setattr(network.Api, "_prepare_request", prepare)
    action = role.prepare("reply", {"event_id": event["id"], "text": "回复"})
    result = await role.execute(action["id"])
    assert result["status"] == "cancelled" and result["sent"] is False
    assert not sent
    with role.store.db() as db:
        assert db.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()[0] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,payload", CASES)
async def test_every_write_uses_only_one_post_on_wbi_rejection(store, monkeypatch, sdk, kind, payload):
    from bilibili_api.exceptions import ResponseCodeException

    network, sent = sdk
    guards = []

    def rejected(*args, **kwargs):
        raise ResponseCodeException(-403, "fake WBI rejection")

    monkeypatch.setattr(network.Api, "_process_response", rejected)
    with pytest.raises(AssistantError) as error:
        await Bilibili(store).perform(kind, payload, {"favorite_folder_id": 12345}, lambda: guards.append(True))
    assert error.value.code == "platform_rejected"
    assert len(sent) == len(guards) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,payload", CASES)
async def test_guarded_write_preserves_request_contract(store, sdk, kind, payload):
    _network, sent = sdk
    result = await Bilibili(store).perform(kind, payload, {"favorite_folder_id": 12345}, lambda: None)
    assert len(sent) == 1
    data = sent[0]["data"]
    if kind in {"comment", "reply"}:
        assert data["message"] == payload["text"]
        assert data["type"] == 1
        if kind == "reply":
            assert (data["root"], data["parent"]) == (111, 222)
        if payload.get("mentions"):
            assert json.loads(data["at_name_to_mid"]) == {"测试": 789}
    elif kind == "dm_reply":
        assert json.loads(data["msg[content]"])["content"] == "私信"
        assert (data["msg[sender_uid]"], data["msg[receiver_id]"]) == (123, 789)
        assert sent[0]["params"]["w_receiver_id"] == 789
    elif kind == "dynamic":
        data = json.loads(data)["dyn_req"]
        assert data["scene"] == 1 and data["content"]["contents"][0]["raw_text"] == "动态"
    elif kind == "favorite":
        assert data["add_media_ids"] == "12345" and data["type"] == 2
    elif kind == "follow":
        assert data["fid"] == 789 and data["act"] == 1
    elif kind == "like":
        assert data["like"] == 1 and result["verified"]
    elif kind == "coin":
        assert data["multiply"] == 1 and data["select_like"] == 0 and result["verified"]


def fake_inbox(monkeypatch, ts, *, conversations=2, notifications=40):
    from bilibili_api import session

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: object())
    visited = []

    def notification(index, field):
        return {field: ts, "user": {"mid": 789}, "item": {
            "type": "reply", "business_id": 1, "subject_id": 999,
            "root_id": index, "source_id": index, "source_content": f"通知{index}",
        }}

    async def replies(_credential):
        return {"items": [notification(i, "reply_time") for i in range(1, notifications // 2 + 1)]}

    async def mentions(_credential):
        return {"items": [notification(i, "at_time") for i in range(notifications // 2 + 1, notifications + 1)]}

    async def sessions(_credential, session_type):
        return {"session_list": [{"talker_id": uid, "session_ts": (ts + 2) * 1_000_000}
                                 for uid in reversed(range(700, 700 + conversations))]}

    async def history(uid, _credential):
        visited.append(uid)
        count = 20 if uid == 700 else 1
        return {"messages": [{"timestamp": ts + 1, "sender_uid": uid,
                              "msg_seqno": i, "msg_type": 1, "content": {"content": f"消息{uid}-{i}"}}
                             for i in range(1, count + 1)]}

    monkeypatch.setattr(session, "get_replies", replies)
    monkeypatch.setattr(session, "get_at", mentions)
    monkeypatch.setattr(session, "get_sessions", sessions)
    monkeypatch.setattr(session, "fetch_session_msgs", history)
    return visited


@pytest.mark.asyncio
async def test_more_than_fifty_notifications_do_not_starve_another_conversation(role, monkeypatch):
    fake_inbox(monkeypatch, int(role.clock().timestamp()) + 10)
    role.adapter = Bilibili(role.store)
    reports = [await role.inbox(archive=False, report=True) for _ in range(3)]
    assert [item["new_count"] for item in reports] == [61, 0, 0]
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 61
        assert db.execute("SELECT 1 FROM events WHERE id='dm-701-1'").fetchone()


@pytest.mark.asyncio
async def test_session_polling_is_bounded_fair_and_persists_across_adapter_restart(role, monkeypatch):
    visited = fake_inbox(monkeypatch, int(role.clock().timestamp()) + 10, conversations=25, notifications=0)
    for _ in range(3):
        role.adapter = Bilibili(role.store)
        await role.inbox(archive=False, report=True)
    assert len(visited) == 30
    assert set(visited) == set(range(700, 725))
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(DISTINCT uid) FROM events").fetchone()[0] == 25


@pytest.mark.asyncio
async def test_fairness_cursor_is_scoped_to_account_and_run(store, monkeypatch):
    fake_inbox(monkeypatch, 1792476010, conversations=15, notifications=0)
    adapter = Bilibili(store)
    started = "2026-10-20T00:00:00+00:00"
    first = await adapter.inbox(started)
    second = await adapter.inbox(started)
    assert {item["uid"] for item in first} != {item["uid"] for item in second}
    restarted = await adapter.inbox("2026-10-20T00:00:01+00:00")
    assert {item["uid"] for item in first} == {item["uid"] for item in restarted}
    manifest = store.read("manifest")
    manifest["data"]["account_uid"] = "456"
    store.write("manifest", manifest["data"], manifest["version"], "test")
    assert {item["uid"] for item in first} == {item["uid"] for item in await adapter.inbox(started)}
