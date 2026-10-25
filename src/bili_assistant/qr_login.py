"""Own the WEB QR response handling; bilibili-api 17.4.2 loses new login cookies."""

from enum import Enum
from http.cookies import CookieError, SimpleCookie
from io import StringIO
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
import qrcode

from .errors import AssistantError

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
LOGIN_HOSTS = frozenset({"passport.bilibili.com", "passport.biligame.com", "www.bilibili.com"})
QR_HOSTS = frozenset({"account.bilibili.com", "passport.bilibili.com"})
COOKIE_FIELDS = {
    "SESSDATA": "sessdata",
    "bili_jct": "bili_jct",
    "DedeUserID": "dedeuserid",
    "buvid3": "buvid3",
    "buvid4": "buvid4",
}


class QrState(Enum):
    WAITING = "waiting"
    SCANNED = "scanned"
    EXPIRED = "expired"
    DONE = "done"


def validate_credential(credential, code="credential_invalid"):
    """Reject empty credentials before profile requests or keyring writes."""
    for field in ("sessdata", "bili_jct"):
        value = getattr(credential, field, None)
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n;"):
            raise AssistantError(code, "登录凭证不完整，请重新运行 login 扫码；未保存空凭证")


def _checked_url(url, hosts=LOGIN_HOSTS):
    if not isinstance(url, str):
        raise AssistantError("login_redirect_invalid", "登录服务返回了不受支持的跳转地址，已停止登录")
    try:
        parsed = urlsplit(url)
        allowed = (
            parsed.scheme == "https"
            and parsed.hostname in hosts
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except (ValueError, TypeError):
        allowed = False
    if not allowed:
        raise AssistantError("login_redirect_invalid", "登录服务返回了不受支持的跳转地址，已停止登录")
    return parsed


def _url_cookies(url):
    parsed = _checked_url(url)
    try:
        pairs = parse_qsl(parsed.query, max_num_fields=64)
    except ValueError:
        raise AssistantError("login_response_invalid", "B站登录返回链接格式异常") from None
    return {
        COOKIE_FIELDS[name]: value
        for name, value in pairs
        if name in COOKIE_FIELDS and value
    }


def _response_cookies(response):
    # Read every Set-Cookie independently: Expires contains commas. Some cross-domain
    # responses set credentials for another Bilibili domain, which a cookie jar drops.
    values = {}
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except CookieError:
            continue
        for name, cookie in parsed.items():
            if name in COOKIE_FIELDS and cookie.value:
                values[COOKIE_FIELDS[name]] = cookie.value
    return values


def _complete(values):
    return bool(values.get("sessdata") and values.get("bili_jct"))


def _data(response):
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or type(body.get("code")) is not int:
        raise AssistantError("login_response_invalid", "B站登录响应格式异常，请稍后重新运行 login")
    if body["code"] != 0:
        raise AssistantError("login_rejected", f"B站登录接口返回错误码 {body['code']}，请稍后重试")
    if not isinstance(body.get("data"), dict):
        raise AssistantError("login_response_invalid", "B站登录响应缺少数据，请稍后重新运行 login")
    return body["data"]


class WebQrLogin:
    def __init__(self, client):
        self.client = client
        self._key = None
        self._terminal = None
        self._credential = None

    async def _get(self, url, **kwargs):
        _checked_url(url)
        try:
            response = await self.client.get(url, follow_redirects=False, **kwargs)
        except httpx.TimeoutException:
            raise AssistantError("login_timeout", "B站登录请求超时，请稍后重新运行 login") from None
        except httpx.HTTPError:
            # HTTPX exception strings may contain the login ticket or QR key.
            raise AssistantError("login_network_error", "无法连接 B站登录服务，请检查网络后重试") from None
        if response.status_code >= 400:
            raise AssistantError(
                "login_http_error", f"B站登录请求失败（HTTP {response.status_code}），请稍后重试"
            )
        return response

    async def generate_qrcode(self):
        response = await self._get(GENERATE_URL, params={"source": "main-fe-header"})
        data = _data(response)
        if not isinstance(data.get("qrcode_key"), str) or not data["qrcode_key"]:
            raise AssistantError("login_response_invalid", "B站登录响应缺少二维码标识")
        url = data.get("url")
        _checked_url(url, QR_HOSTS)
        self._key = data["qrcode_key"]
        qr = qrcode.QRCode()
        qr.add_data(url)
        output = StringIO()
        qr.print_ascii(out=output, invert=True)
        self._terminal = output.getvalue()

    def get_qrcode_terminal(self):
        if self._terminal is None:
            raise AssistantError("qr_not_ready", "二维码尚未生成")
        return self._terminal

    async def _resolve_credential(self, data, response):
        from bilibili_api import Credential

        values = _response_cookies(response)
        url = data.get("url")
        if not _complete(values) and url:
            # Legacy query parameters are a fallback; response cookies take precedence.
            values = {**_url_cookies(url), **values}
        for _ in range(5):
            if _complete(values) or not url:
                break
            # No automatic redirect: validate each destination before sending the ticket.
            response = await self._get(url)
            values.update(_response_cookies(response))
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location")
            if not location or _complete(values):
                break
            url = urljoin(str(response.url), location)
            values.update(_url_cookies(url))
        refresh_token = data.get("refresh_token")
        if isinstance(refresh_token, str):
            values["ac_time_value"] = refresh_token
        credential = Credential(**values)
        validate_credential(credential, "login_credentials_incomplete")
        return credential

    async def check_state(self):
        if self._credential is not None:
            return QrState.DONE  # A successful QR key is single-use; never poll it again.
        if self._key is None:
            raise AssistantError("qr_not_ready", "二维码尚未生成")
        response = await self._get(POLL_URL, params={"qrcode_key": self._key, "source": "main-fe-header"})
        data = _data(response)
        code = data.get("code")
        if type(code) is not int:
            raise AssistantError("login_response_invalid", "B站登录响应缺少状态码")
        waiting = {86101: QrState.WAITING, 86090: QrState.SCANNED, 86038: QrState.EXPIRED}
        if code in waiting:
            return waiting[code]
        if code != 0:
            raise AssistantError("login_rejected", f"B站扫码登录返回错误码 {code}，请重新运行 login")
        self._credential = await self._resolve_credential(data, response)
        return QrState.DONE

    def get_credential(self):
        if self._credential is None:
            raise AssistantError("login_incomplete", "扫码登录尚未完成")
        return self._credential
