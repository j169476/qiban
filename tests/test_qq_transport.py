import json

import httpx
import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyScope
from companion_qq.config import validate
from companion_qq.normalization import normalize
from companion_qq.transport import OneBot
from tests_postgres.test_qq_module import OPTIONS, incoming

TOKEN = "synthetic-token-for-local-unit-tests"


def provider(handler):
    result = OneBot(OPTIONS, TOKEN)
    result.http.close()
    result.http = httpx.Client(transport=httpx.MockTransport(handler))
    return result


def test_probe_checks_identity_twice_and_sends_only_structured_text():
    calls = []
    def handler(request):
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        data = {"/get_login_info": {"user_id": 10001, "nickname": "synthetic"},
                "/get_status": {"online": True, "good": True}, "/get_friend_list": [{"user_id": 10002}],
                "/get_group_list": [{"group_id": 10003}],
                "/get_group_member_info": {"user_id": 10001, "group_id": 10003},
                "/send_private_msg": {"message_id": -123}}
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data[request.url.path]})
    connection = provider(handler)
    try:
        assert connection.probe()["account"] == "10001"
        assert sum(path == "/get_login_info" for path, _ in calls) == 2
        text = "[CQ:at,qq=all] is literal text"
        assert connection.send(ReplyScope("qq", "10001", "private", "10002"), text) == "-123"
        assert calls[-1] == ("/send_private_msg", {"user_id": 10002,
                            "message": [{"type": "text", "data": {"text": text}}]})
    finally:
        connection.close()


@pytest.mark.parametrize("status,body", [(302, {}), (200, {"status": "failed", "retcode": 1, "message": "secret"}),
                                        (200, {"status": "ok", "retcode": 0, "data": {}})])
def test_send_rejection_or_missing_receipt_is_not_retried(status, body):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body, headers={"Location": "https://example.invalid"})
    connection = provider(handler)
    try:
        with pytest.raises(AssistantError) as error:
            connection.send(ReplyScope("qq", "10001", "private", "10002"), "synthetic")
        assert len(calls) == 1 and "secret" not in str(error.value)
    finally:
        connection.close()


@pytest.mark.parametrize("endpoint", ["http://0.0.0.0:3000", "http://localhost:3000",
    "http://127.0.0.1:3000/?access_token=secret", "https://127.0.0.1:3000", "http://user:pw@127.0.0.1:3000"])
def test_only_literal_loopback_endpoints_are_allowed(endpoint):
    with pytest.raises(AssistantError):
        validate(OPTIONS | {"http": endpoint})


def test_temporary_private_sessions_unknown_groups_and_text_cq_do_not_inherit_permissions():
    def parse(raw):
        return normalize(raw, OPTIONS, sent_reference=lambda *_: False)
    assert parse(incoming(1) | {"sub_type": "group"}) is None
    assert parse(incoming(1, group=True) | {"group_id": 99999}) is None
    event = parse(incoming(1, group=True, text="[CQ:at,qq=10001] @龙娘"))
    assert event.metadata["qq"]["trigger"] == "none"
    with pytest.raises(AssistantError):
        parse(incoming(1) | {"self_id": 99999})


@pytest.mark.asyncio
async def test_websocket_uses_loopback_bearer_auth_and_bounded_frames():
    from websockets.asyncio.server import serve

    observed = []
    async def handler(socket):
        observed.append(socket.request.headers.get("Authorization"))
        await socket.send(json.dumps(incoming(1)))
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        connection = OneBot(OPTIONS | {"ws": f"ws://127.0.0.1:{port}"}, TOKEN)
        try:
            socket = await connection.connect()
            assert json.loads(await socket.recv())["message_id"] == 1
            await socket.close()
            assert observed == ["Bearer " + TOKEN]
        finally:
            connection.close()


def test_second_logged_in_account_cannot_be_used_as_the_bot_endpoint():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                       "data": {"user_id": 10002, "nickname": "other account"}})
    connection = provider(handler)
    try:
        with pytest.raises(AssistantError) as error:
            connection.probe()
        assert error.value.code == "qq_account_mismatch"
        assert calls == ["/get_login_info"]
        for scope in (ReplyScope("qq", "10002", "private", "10002"),
                      ReplyScope("qq", "10001", "private", "10003")):
            with pytest.raises(AssistantError) as denied:
                connection.send(scope, "synthetic")
            assert denied.value.code == "qq_target_unverified"
        assert calls == ["/get_login_info"]
    finally:
        connection.close()


def test_identical_message_numbers_on_two_accounts_have_distinct_event_and_actor_ids():
    first = normalize(incoming(123), OPTIONS, sent_reference=lambda *_: False)
    second = normalize(incoming(123) | {"self_id": 10004}, OPTIONS | {"account": "10004"},
                       sent_reference=lambda *_: False)
    assert first.id != second.id and first.actor.key != second.actor.key
    assert first.thread != second.thread
