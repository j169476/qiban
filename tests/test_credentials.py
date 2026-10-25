"""QR login contracts use synthetic HTTP responses, never a real account/keyring."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote

import httpx
import pytest

from bili_assistant import credentials
from bili_assistant.errors import AssistantError
from bili_assistant.qr_login import GENERATE_URL, HEADERS, POLL_URL, QrState, WebQrLogin

QR_URL = "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=fake-key"
CROSS_URL = "https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=fake-ticket"
SESSION = "fake%2Csession%2Bvalue"
COOKIE_HEADERS = [
    ("set-cookie", f"SESSDATA={SESSION}; Domain=.bilibili.com; Path=/; HttpOnly"),
    ("set-cookie", "bili_jct=fake-csrf; Domain=.bilibili.com; Expires=Wed, 21 Oct 2026 00:00:00 GMT"),
    ("set-cookie", "DedeUserID=456; Domain=.bilibili.com; Path=/"),
]


def envelope(data, headers=()):
    return httpx.Response(200, json={"code": 0, "data": data}, headers=headers)


def transport_for(poll, callback=None):
    requests = []

    def respond(request):
        requests.append(request)
        if str(request.url).split("?")[0] == GENERATE_URL:
            return envelope({"url": QR_URL, "qrcode_key": "fake-key"})
        if str(request.url).split("?")[0] == POLL_URL:
            assert request.url.params["qrcode_key"] == "fake-key"
            return poll(request) if callable(poll) else poll
        assert callback is not None, "Unexpected follow-up request"
        return callback(request)

    return httpx.MockTransport(respond), requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [QR_URL, "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=fake-key"],
)
async def test_new_and_legacy_qr_page_domains_render_without_being_requested(url):
    requests = []

    def respond(request):
        requests.append(request)
        return envelope({"url": url, "qrcode_key": "fake-key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        assert flow.get_qrcode_terminal().strip()
    assert len(requests) == 1
    assert requests[0].url.host == "passport.bilibili.com"


@pytest.mark.asyncio
async def test_poll_headers_capture_credentials_without_repoll_or_ticket_request():
    transport, requests = transport_for(
        envelope({"code": 0, "url": CROSS_URL, "refresh_token": "fake-refresh"}, COOKIE_HEADERS)
    )
    async with httpx.AsyncClient(transport=transport, headers=HEADERS) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        assert flow.get_qrcode_terminal().strip()
        assert await flow.check_state() == QrState.DONE
        credential = flow.get_credential()
        assert credential.sessdata == SESSION
        assert credential.bili_jct == "fake-csrf"
        assert credential.dedeuserid == "456"
        assert credential.ac_time_value == "fake-refresh"
        assert await flow.check_state() == QrState.DONE
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_cross_domain_ticket_collects_all_headers_through_allowed_redirects():
    def callback(request):
        assert request.headers["user-agent"].startswith("Mozilla/")
        if request.url.host == "passport.biligame.com":
            return httpx.Response(
                302,
                headers=[
                    ("location", "https://passport.bilibili.com/x/passport-login/web/crossDomain?ticket=fake-next"),
                    COOKIE_HEADERS[2],
                ],
            )
        return httpx.Response(302, headers=[("location", "https://www.bilibili.com/"), *COOKIE_HEADERS[:2]])

    transport, requests = transport_for(
        envelope({"code": 0, "url": CROSS_URL, "refresh_token": "fake-refresh"}), callback
    )
    async with httpx.AsyncClient(transport=transport, headers=HEADERS) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        assert await flow.check_state() == QrState.DONE
        credential = flow.get_credential()
        assert credential.sessdata == SESSION
        assert credential.bili_jct == "fake-csrf"
        assert credential.dedeuserid == "456"
    assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", ["fake%2Csession%2Bvalue", "fake%252Csession%252Bvalue"])
async def test_legacy_callback_parses_uid_and_encoded_session_once(encoded):
    url = f"https://passport.bilibili.com/callback?DedeUserID=456&SESSDATA={encoded}&bili_jct=fake-csrf"
    transport, requests = transport_for(envelope({"code": 0, "url": url}))
    async with httpx.AsyncClient(transport=transport) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        assert await flow.check_state() == QrState.DONE
        assert unquote(flow.get_credential().sessdata) == "fake,session+value"
        assert flow.get_credential().dedeuserid == "456"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_poll_cookie_takes_precedence_over_legacy_url_value():
    url = "https://passport.bilibili.com/callback?SESSDATA=old-value&bili_jct=fake-csrf&DedeUserID=456"
    transport, _ = transport_for(envelope({"code": 0, "url": url}, COOKIE_HEADERS[:1]))
    async with httpx.AsyncClient(transport=transport) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        await flow.check_state()
        assert flow.get_credential().sessdata == SESSION


@pytest.mark.asyncio
@pytest.mark.parametrize("code,state", [(86101, QrState.WAITING), (86090, QrState.SCANNED), (86038, QrState.EXPIRED)])
async def test_waiting_scan_and_expiry_never_create_credentials(code, state):
    transport, requests = transport_for(envelope({"code": code}))
    async with httpx.AsyncClient(transport=transport) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        assert await flow.check_state() == state
        with pytest.raises(AssistantError) as failure:
            flow.get_credential()
        assert failure.value.code == "login_incomplete"
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poll,error",
    [
        (envelope({"code": 12345, "url": CROSS_URL}), "login_rejected"),
        (envelope({"code": "0"}), "login_response_invalid"),
        (envelope({"code": 0, "url": ""}), "login_credentials_incomplete"),
        (envelope({"code": 0}, COOKIE_HEADERS[:1]), "login_credentials_incomplete"),
        (httpx.Response(412, text="fake-secret"), "login_http_error"),
        (httpx.Response(200, text="fake-secret"), "login_response_invalid"),
        (httpx.Response(200, json={"code": -101, "message": "fake-secret"}), "login_rejected"),
    ],
)
async def test_invalid_responses_fail_without_exposing_response_or_treating_them_as_done(poll, error):
    transport, _ = transport_for(poll)
    async with httpx.AsyncClient(transport=transport) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        with pytest.raises(AssistantError) as failure:
            await flow.check_state()
        assert failure.value.code == error
        assert "fake-secret" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/callback?ticket=fake-secret",
        "https://passport.bilibili.com.evil.example/?ticket=fake-secret",
        "http://passport.bilibili.com/?ticket=fake-secret",
        "https://evil.example@passport.bilibili.com/?ticket=fake-secret",
        "https://passport.bilibili.com:444/?ticket=fake-secret",
    ],
)
@pytest.mark.parametrize("from_redirect", [False, True])
async def test_untrusted_ticket_and_redirect_destinations_are_never_requested(url, from_redirect):
    transport, requests = transport_for(
        envelope({"code": 0, "url": CROSS_URL if from_redirect else url}),
        lambda request: httpx.Response(302, headers={"location": url}),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        with pytest.raises(AssistantError) as failure:
            await flow.check_state()
        assert failure.value.code == "login_redirect_invalid"
        assert "fake-secret" not in str(failure.value)
    assert len(requests) == (3 if from_redirect else 2)


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded():
    transport, requests = transport_for(
        envelope({"code": 0, "url": CROSS_URL}),
        lambda request: httpx.Response(302, headers={"location": CROSS_URL}),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        flow = WebQrLogin(client)
        await flow.generate_qrcode()
        with pytest.raises(AssistantError) as failure:
            await flow.check_state()
        assert failure.value.code == "login_credentials_incomplete"
    assert len(requests) == 7


@pytest.fixture
def login_environment(monkeypatch):
    """Drive the actual login orchestration over mock HTTP, with an in-memory keyring."""
    from bilibili_api import user

    profile = AsyncMock(return_value={"mid": 456, "name": "测试账号"})
    monkeypatch.setattr(user, "get_self_info", profile)
    vault = {}
    writes = []

    def set_password(service, key, value):
        writes.append((service, key))
        vault[(service, key)] = value

    monkeypatch.setattr(credentials.keyring, "set_password", set_password)
    monkeypatch.setattr(credentials.keyring, "get_password", lambda service, key: vault.get((service, key)))

    def install(poll, callback=None):
        transport, requests = transport_for(poll, callback)
        clients = []

        def make_client(**kwargs):
            client = httpx.AsyncClient(transport=transport, **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr(credentials, "httpx", SimpleNamespace(AsyncClient=make_client))
        return requests, clients

    return SimpleNamespace(install=install, profile=profile, vault=vault, writes=writes)


@pytest.mark.asyncio
async def test_success_verifies_before_saving_and_binds_account(store, login_environment, capsys):
    env = login_environment
    _, clients = env.install(envelope({"code": 0, "url": CROSS_URL}, COOKIE_HEADERS))

    async def verify(credential):
        assert not env.writes
        assert credential.sessdata == SESSION
        assert store.read("manifest")["data"]["account_uid"] == "123"
        return {"mid": 456, "name": "测试账号"}

    env.profile.side_effect = verify
    assert await credentials.login(store) == {"logged_in": True, "uid": "456", "name": "测试账号"}
    assert store.read("manifest")["data"]["account_uid"] == "456"
    assert credentials.load(store).sessdata == SESSION
    assert len(env.writes) == 1
    assert clients[0].is_closed
    output = capsys.readouterr().out
    assert SESSION not in output and "fake-csrf" not in output
    assert "fake-ticket" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "profile_error", "account_mismatch", "invalid_profile", "expired"])
async def test_failed_login_preserves_previous_account_and_keyring(store, login_environment, failure):
    env = login_environment
    before = store.read("manifest")
    saved_key = (credentials.SERVICE, credentials.key(store))
    env.vault[saved_key] = json.dumps({"sessdata": "old-session", "bili_jct": "old-csrf"})
    old_secret = env.vault[saved_key]
    poll = envelope({"code": 0, "url": CROSS_URL}, COOKIE_HEADERS)
    if failure == "incomplete":
        poll = envelope({"code": 0, "url": ""})
    elif failure == "expired":
        poll = envelope({"code": 86038})
    elif failure == "profile_error":
        env.profile.side_effect = RuntimeError("fake-secret-cookie")
    elif failure == "account_mismatch":
        env.profile.return_value = {"mid": 789, "name": "different"}
    else:
        env.profile.return_value = {"mid": None}
    _, clients = env.install(poll)
    with pytest.raises(AssistantError) as error:
        await credentials.login(store)
    assert "fake-secret-cookie" not in str(error.value)
    assert store.read("manifest") == before
    assert env.vault[saved_key] == old_secret
    assert not env.writes
    assert clients[0].is_closed
    if failure in ("incomplete", "expired"):
        env.profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_failure_is_sanitized(store, login_environment):
    def poll(request):
        raise httpx.ConnectError(f"fake-secret {request.url}", request=request)

    login_environment.install(poll)
    with pytest.raises(AssistantError) as error:
        await credentials.login(store)
    assert error.value.code == "login_network_error"
    assert "fake-secret" not in str(error.value) and "fake-key" not in str(error.value)
    assert not login_environment.writes


def test_empty_saved_credentials_are_rejected_even_in_optional_mode(store, login_environment):
    saved_key = (credentials.SERVICE, credentials.key(store))
    login_environment.vault[saved_key] = json.dumps({"sessdata": "", "bili_jct": ""})
    with pytest.raises(AssistantError) as error:
        credentials.load(store, optional=True)
    assert error.value.code == "credential_invalid"
    with pytest.raises(AssistantError):
        credentials.save(store, SimpleNamespace(sessdata="", bili_jct="fake-csrf"))
    assert not login_environment.writes
