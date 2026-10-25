from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bilibili_api import Credential, video
from bilibili_api.exceptions import ResponseCodeException
from bilibili_api.utils import network

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError


@pytest.mark.asyncio
async def test_video_post_uses_select_like_origin_and_guard_after_preparation(store, monkeypatch):
    calls = []
    credential = Credential(sessdata="fake-session", bili_jct="fake-csrf", buvid3="fake3", buvid4="fake4")
    item = video.Video(bvid="BV1RT4y1h7YY", credential=credential)

    async def prepare(api):
        calls.append(("prepare", api.data.copy(), api.headers.copy()))
        return {"method": "POST", "url": api.url, "data": api.data}

    async def request(**kwargs):
        calls.append(("post", kwargs))
        return object()

    monkeypatch.setattr(network.Api, "_prepare_request", prepare)
    monkeypatch.setattr(network.Api, "_process_response", lambda *_: {})
    monkeypatch.setattr(network, "get_client", lambda: SimpleNamespace(request=request))
    result = await Bilibili(store)._video_post(
        "coin", item, credential, {"multiply": 1, "select_like": 0}, lambda: calls.append(("guard",))
    )
    assert result == {}
    assert [call[0] for call in calls] == ["prepare", "guard", "post"]
    assert calls[0][1]["select_like"] == 0 and "like" not in calls[0][1]
    assert calls[0][2]["Origin"] == "https://www.bilibili.com"
    assert calls[0][2]["Referer"] == "https://www.bilibili.com/video/BV1RT4y1h7YY/"

    calls.clear()

    def cancelled():
        raise AssistantError("before_send_cancelled", "fixture archive")

    with pytest.raises(AssistantError):
        await Bilibili(store)._video_post("like", item, credential, {"like": 1}, cancelled)
    assert [call[0] for call in calls] == ["prepare"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["like", "coin"])
async def test_failed_confirmation_after_success_is_uncertain_not_retriable_rejection(role, monkeypatch, kind):
    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *_: object())
    item = SimpleNamespace(
        has_liked=AsyncMock(side_effect=[0, ResponseCodeException(-101, "fake-secret")]),
        get_pay_coins=AsyncMock(side_effect=[0, ResponseCodeException(-101, "fake-secret")]),
    )
    monkeypatch.setattr(video, "Video", lambda **_: item)
    adapter = Bilibili(role.store)
    adapter._video_post = AsyncMock(return_value={})
    adapter.me = AsyncMock(return_value={"uid": "123"})
    role.adapter = adapter
    role.store.set_state("context:BV1RT4y1h7YY", {"owner_uid": "888", "coverage": {"kind": "metadata_only"}})
    payload = {"bvid": "BV1RT4y1h7YY", **({"units": 1} if kind == "coin" else {})}
    action = role.prepare(kind, payload)
    result = await role.execute(action["id"])
    assert result["status"] == "uncertain"
    assert result["result"]["error"] == "action_unconfirmed"
    assert "fake-secret" not in str(result)
    assert not (await role.execute(action["id"]))["sent"]
    assert adapter._video_post.await_count == 1


@pytest.mark.asyncio
async def test_video_post_rejection_is_not_automatically_retried(store, monkeypatch):
    credential = Credential(sessdata="fake-session", bili_jct="fake-csrf", buvid3="fake3", buvid4="fake4")
    item = video.Video(bvid="BV1RT4y1h7YY", credential=credential)
    request = AsyncMock(return_value=object())
    monkeypatch.setattr(network.Api, "_prepare_request", AsyncMock(return_value={"method": "POST"}))
    monkeypatch.setattr(network, "get_client", lambda: SimpleNamespace(request=request))

    def rejected(*_):
        raise ResponseCodeException(-403, "denied")

    monkeypatch.setattr(network.Api, "_process_response", rejected)
    with pytest.raises(ResponseCodeException):
        await Bilibili(store)._video_post("like", item, credential, {"like": 1}, lambda: None)
    assert request.await_count == 1
