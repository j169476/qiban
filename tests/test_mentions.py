import json
from types import SimpleNamespace

import pytest

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError
from bili_assistant.mentions import validate_mentions


@pytest.fixture
def prepared_transport(monkeypatch):
    from bilibili_api import Credential
    from bilibili_api.utils import network

    import bili_assistant.credentials as credentials

    credential = Credential(sessdata="fake", bili_jct="fake", buvid3="fake3", buvid4="fake4")
    monkeypatch.setattr(credentials, "load", lambda *args: credential)
    calls, trace, response = [], [], {}
    prepare = network.Api._prepare_request

    async def ticket(_credential):
        return "fake-ticket", "9999999999"

    async def wbi(_credential):
        return "0123456789abcdef0123456789abcdef"

    async def prepared(api):
        result = await prepare(api)
        trace.append("prepared")
        return result

    class Client:
        async def request(self, **request):
            assert trace == ["prepared", "guard"]
            assert request["method"] == "POST"
            calls.append(request)
            trace.append("post")
            return SimpleNamespace(
                code=200, headers={},
                utf8_text=lambda: json.dumps({"code": 0, "data": response}),
            )

    def guard():
        assert trace == ["prepared"]
        trace.append("guard")

    monkeypatch.setattr(network, "get_bili_ticket", ticket)
    monkeypatch.setattr(network, "get_wbi_mixin_key", wbi)
    monkeypatch.setattr(network, "get_client", lambda: Client())
    monkeypatch.setattr(network.Api, "_prepare_request", prepared)
    return calls, trace, response, guard


@pytest.mark.parametrize(
    "text,mentions",
    [
        ("测试 @功能测试 @主号 ", {"主号": "888"}),
        ("你好 @主号 ", {}),
        ("你好 @主号！", {"主号": "888"}),
        ("没有提及", {"主号": "888"}),
        ("@主号 ", {"主号": "not-a-uid"}),
    ],
)
def test_accidental_or_invalid_mentions_are_rejected(text, mentions):
    with pytest.raises(AssistantError):
        validate_mentions(text, mentions)


def test_known_mention_and_plain_text():
    assert validate_mentions("你好 @主号 谢谢", {"主号": "888"}) == {"主号": "888"}
    assert validate_mentions("普通回复", {}) == {}


@pytest.mark.asyncio
async def test_mention_cannot_target_unrelated_user(role):
    role.store.set_state(
        "context:BV1xx411c7mD",
        {
            "owner_uid": "888",
            "title": "视频",
            "coverage": {"kind": "subtitles"},
        },
    )
    action = role.prepare(
        "comment",
        {
            "bvid": "BV1xx411c7mD",
            "text": "你好 @陌生人 ",
            "mentions": {"陌生人": "777"},
        },
    )
    with pytest.raises(AssistantError) as exc:
        await role.execute(action["id"])
    assert exc.value.code == "ungrounded_mention"
    assert role.adapter.sent == []


@pytest.mark.asyncio
async def test_adapter_sends_exact_uid_mapping_and_reply_coordinates(store, prepared_transport):
    calls, trace, response, guard = prepared_transport
    response.update(rpid=42)
    result = await Bilibili(store).perform(
        "reply",
        {"text": "你好 @主号 ", "mentions": {"主号": "888"}, "_event": {"oid": 9, "root": 10, "parent": 11}},
        {},
        guard,
    )
    assert result == {"rpid": 42}
    assert len(calls) == 1 and trace == ["prepared", "guard", "post"]
    assert calls[0]["url"] == "https://api.bilibili.com/x/v2/reply/add"
    data = calls[0]["data"]
    assert json.loads(data["at_name_to_mid"]) == {"主号": 888}
    assert (data["oid"], data["root"], data["parent"], data["type"]) == (9, 10, 11, 1)
    assert data["message"] == "你好 @主号 "


def test_dynamic_mentions_require_observed_video_target(role):
    with pytest.raises(AssistantError) as exc:
        role.prepare("dynamic", {"text": "@主号 测试", "mentions": {"主号": "888"}})
    assert exc.value.code == "ungrounded_mention"


@pytest.mark.asyncio
async def test_dynamic_uses_structured_mention(store, prepared_transport):
    calls, trace, response, guard = prepared_transport
    response.update(dyn_id_str="123")
    result = await Bilibili(store).perform(
        "dynamic",
        {"text": "测试 @主号 完成", "mentions": {"主号": "888"}},
        {},
        guard,
    )
    assert result == {"dynamic_id_str": "123"}
    assert len(calls) == 1 and trace == ["prepared", "guard", "post"]
    assert calls[0]["url"] == "https://api.bilibili.com/x/dynamic/feed/create/dyn"
    data = json.loads(calls[0]["data"])["dyn_req"]
    assert data["scene"] == 1
    contents = data["content"]["contents"]
    assert [part["raw_text"] for part in contents] == ["测试 ", "@主号", " 完成"]
    assert contents[1]["biz_id"] == "888" and contents[1]["type"] == 2
