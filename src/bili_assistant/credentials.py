"""Credentials only live in the OS keyring, never in the data directory."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import keyring
from filelock import FileLock, Timeout

from .errors import AssistantError
from .qr_login import HEADERS, QrState, WebQrLogin, validate_credential
from .storage import utcnow

SERVICE = "codex-bilibili-assistant"
FIELDS = ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")


def key(store):
    manifest = store.read("manifest")["data"]
    return f"{manifest['project_id']}:{manifest['credential_ref']}"


def load(store, optional=False):
    from bilibili_api import Credential

    try:
        raw = keyring.get_password(SERVICE, key(store))
    except Exception:
        raise AssistantError("keyring_unavailable", "系统凭证存储不可用；不回退到明文文件") from None
    if not raw:
        if optional:
            return Credential()
        raise AssistantError("login_required", "尚未登录，请在本地终端运行 login")
    try:
        credential = Credential(**json.loads(raw))
        validate_credential(credential)
        return credential
    except Exception:
        raise AssistantError("credential_invalid", "本机凭证无效，请重新登录") from None


def save(store, credential):
    with FileLock(str(store.runtime / "credential.lock"), timeout=20):
        _save(store, credential)


def _save(store, credential):
    validate_credential(credential)
    data = {field: getattr(credential, field, None) for field in FIELDS}
    try:
        keyring.set_password(SERVICE, key(store), json.dumps(data))
    except Exception:
        raise AssistantError("keyring_unavailable", "无法保存到系统凭证存储") from None


async def login(store):
    from bilibili_api import user

    original = store.read("manifest")
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=False) as client:
        flow = WebQrLogin(client)
        try:
            await asyncio.wait_for(flow.generate_qrcode(), 30)
            print(flow.get_qrcode_terminal(), flush=True)
            print("请使用 B站手机 App 扫码并确认登录。", flush=True)
            prompted = False
            async with asyncio.timeout(180):
                while True:
                    status = await flow.check_state()
                    if status == QrState.DONE:
                        break
                    if status == QrState.EXPIRED:
                        raise AssistantError("qr_expired", "二维码已过期，请重新运行 login")
                    if status == QrState.SCANNED and not prompted:
                        print("已扫码，请在手机上确认登录。", flush=True)
                        prompted = True
                    await asyncio.sleep(2)
        except TimeoutError:
            raise AssistantError("login_timeout", "登录等待超时，请重新运行 login") from None
        credential = flow.get_credential()
    validate_credential(credential, "login_credentials_incomplete")
    print("扫码已确认，正在验证账号…", flush=True)
    try:
        info = await asyncio.wait_for(user.get_self_info(credential), 20)
    except Exception as exc:
        # Never include raw library errors: they may contain cookies or ticket URLs.
        status = getattr(exc, "status", None)
        detail = f"（HTTP {status}）" if type(status) is int else ""
        raise AssistantError(
            "login_verification_failed", f"已取得扫码凭证，但账号验证失败{detail}；未保存，请稍后重新运行 login"
        ) from None
    if (
        not isinstance(info, dict)
        or type(info.get("mid")) is not int
        or info["mid"] <= 0
        or not isinstance(info.get("name"), str)
    ):
        raise AssistantError("login_verification_failed", "账号验证响应不完整；未保存，请稍后重新运行 login")
    uid = str(info["mid"])
    if credential.dedeuserid and credential.dedeuserid != uid:
        raise AssistantError("login_account_mismatch", "扫码凭证与验证账号不一致；未保存，请重新运行 login")
    credential.dedeuserid = uid
    async with credential_lock(store):
        with store.lock:
            current = store.read("manifest")
            if current["version"] != original["version"]:
                raise AssistantError("account_changed", "登录过程中账号配置被修改，请重新登录")
            _save(store, credential)
            current["data"]["account_uid"] = uid
            store.write("manifest", current["data"], current["version"], "local-login", allow_locked=True)
            store.set_state(f"credential-health:{uid}", {"status": "logged_in", "at": utcnow()})
    return {"logged_in": True, "uid": uid, "name": info["name"]}


@asynccontextmanager
async def credential_lock(store):
    lock = FileLock(str(store.runtime / "credential.lock"), timeout=0)
    for _ in range(100):
        try:
            lock.acquire()
            break
        except Timeout:
            await asyncio.sleep(0.1)
    else:
        raise AssistantError("credential_busy", "正在验证或更新凭证，本轮稍后再试")
    try:
        yield
    finally:
        lock.release()


async def maintain(store, account, now=None):
    """Use the pinned SDK's refresh protocol; never store tokens in runtime state."""
    from bilibili_api import user

    now = now or datetime.now(timezone.utc)
    health_key = f"credential-health:{account}"
    async with credential_lock(store):
        manifest = store.read("manifest")
        if manifest["data"]["account_uid"] != account:
            raise AssistantError("account_changed", "登录检查时发现绑定账号已改变")
        previous = store.state(health_key, {})
        if previous.get("status") in ("refreshing", "refresh_uncertain"):
            raise AssistantError("credential_refresh_uncertain", "上次续期未确认完成，需要重新登录")
        settings = store.read("companion")["data"]
        if previous.get("next_check_at"):
            if now < datetime.fromisoformat(previous["next_check_at"]):
                return previous
        credential = load(store)

        async def verify():
            try:
                info = await asyncio.wait_for(user.get_self_info(credential), 20)
            except Exception as exc:
                code = "login_required" if getattr(exc, "code", None) in (-101, -111) else "credential_check_failed"
                raise AssistantError(code, "B站登录检查未通过；未记录原始接口响应") from None
            if (
                not isinstance(info, dict) or type(info.get("mid")) is not int
                or str(info["mid"]) != account
                or credential.dedeuserid and str(credential.dedeuserid) != account
            ):
                raise AssistantError("account_mismatch", "凭证账号与项目绑定账号不一致")

        await verify()
        try:
            needs_refresh = await asyncio.wait_for(credential.check_refresh(), 20)
            if type(needs_refresh) is not bool:
                raise ValueError()
        except Exception as exc:
            code = "login_required" if getattr(exc, "code", None) == -101 else "credential_check_failed"
            raise AssistantError(code, "凭证续期状态检查未成功，可在后续轮次重试") from None
        if needs_refresh:
            if not settings.get("credential_auto_refresh", True) or not credential.ac_time_value:
                raise AssistantError("credential_refresh_required", "平台要求续期，需要重新登录")
            store.set_state(health_key, {"status": "refreshing", "at": now.isoformat()})
            try:
                await asyncio.wait_for(credential.refresh(), 60)
                validate_credential(credential)
                await verify()
                with store.lock:
                    if store.read("manifest")["version"] != manifest["version"]:
                        raise AssistantError("account_changed", "续期过程中账号配置被修改")
                    _save(store, credential)
            except Exception:
                store.set_state(health_key, {"status": "refresh_uncertain", "at": utcnow()})
                # A refresh POST might have succeeded; retrying an old token is unsafe.
                raise AssistantError("credential_refresh_uncertain", "续期结果未确认，请重新登录") from None
        result = {
            "status": "refreshed" if needs_refresh else "valid",
            "at": now.isoformat(),
            "next_check_at": (now + timedelta(hours=settings.get("credential_check_hours", 6))).isoformat(),
            "refresh_available": bool(credential.ac_time_value),
        }
        store.set_state(health_key, result)
        return result
