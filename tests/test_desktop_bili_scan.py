import base64

import httpx
import pytest

from bili_assistant.qr_login import GENERATE_URL, POLL_URL
from companion_core.errors import AssistantError
from qiban.desktop_bili import scan_account


@pytest.mark.parametrize("verified_uid", [101, 202])
async def test_desktop_qr_verifies_identity_without_rendering_cookie_or_following_ticket(verified_uid):
    screens, urls = [], []
    def respond(request):
        url = str(request.url).split("?")[0]
        urls.append(url)
        if url==GENERATE_URL:
            data = {"url":"https://account.bilibili.com/scan?key=synthetic","qrcode_key":"test-key"}
        elif url==POLL_URL:
            return httpx.Response(200,json={"code":0,"data":{"code":0,"url":"","refresh_token":"test-refresh"}},
                                  headers=[("set-cookie","SESSDATA=synthetic-session"),("set-cookie","bili_jct=test-csrf"),
                                           ("set-cookie","DedeUserID=101")])
        else:
            assert url=="https://api.bilibili.com/x/space/myinfo"
            assert "SESSDATA=synthetic-session" in request.headers["cookie"]
            data = {"mid":verified_uid,"name":"测试昵称"}
        return httpx.Response(200,json={"code":0,"data":data})
    job = scan_account(lambda *args:screens.append(args), transport=httpx.MockTransport(respond))
    if verified_uid==101:
        assert (await job)["name"]=="测试昵称"
    else:
        with pytest.raises(AssistantError,match="核对"):
            await job
    assert len(urls)==3 and len(screens)==1
    assert screens[0][0]=="waiting" and base64.b64decode(screens[0][1]).startswith(b"\x89PNG")
