"""Bilibili login owned by the unlocked desktop, independent of role execution."""
import asyncio
import base64
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
from filelock import FileLock, Timeout

from bili_assistant.browser_login_transport import crypt
from bili_assistant.qr_login import HEADERS, QrState, WebQrLogin, validate_credential
from companion_core.errors import AssistantError
from companion_core.storage import atomic_write


async def scan_account(publish, *, transport=None):
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=False, transport=transport) as client:
        flow = WebQrLogin(client, SimpleNamespace)
        await flow.generate_qrcode()
        publish("waiting", base64.b64encode(flow.get_qrcode_png()).decode("ascii"))
        while True:
            status = await flow.check_state()
            if status == QrState.EXPIRED:
                raise AssistantError("qr_expired", "二维码已过期，请重新登录。")
            if status == QrState.DONE:
                break
            if status == QrState.SCANNED:
                publish("scanned")
            await asyncio.sleep(2)
        credential = flow.get_credential()
        validate_credential(credential)
        fields = vars(credential).copy()
    account = await verify_identity(fields,transport=transport)
    fields["dedeuserid"] = account["uid"]
    return {**account, "cookies":fields}


async def verify_identity(fields, *, transport=None):
    names = {"sessdata":"SESSDATA", "bili_jct":"bili_jct", "buvid3":"buvid3",
             "buvid4":"buvid4", "dedeuserid":"DedeUserID"}
    cookies = {name: fields[key] for key,name in names.items() if fields.get(key)}
    async with httpx.AsyncClient(headers=HEADERS,timeout=15,follow_redirects=False,
                                 transport=transport,cookies=cookies) as client:
        response = await client.get("https://api.bilibili.com/x/space/myinfo")
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload,dict) and payload.get("code") == -101:
        raise AssistantError("bili_login_expired","B 站登录已失效，请重新登录。")
    info = payload.get("data") if isinstance(payload,dict) and payload.get("code") == 0 else None
    if (not isinstance(info,dict) or type(info.get("mid")) is not int or info["mid"]<=0
            or not isinstance(info.get("name"),str) or not info["name"].strip()
            or fields.get("dedeuserid") not in (None,"",str(info["mid"]))):
        raise AssistantError("bili_identity","无法核对 B 站账号身份，请重新登录。")
    return {"uid":str(info["mid"]),"name":info["name"]}


def identity(account):
    return {"uid":account["uid"], "name":account["name"]} if account else None


async def verify_sdk_identity(fields):
    from bilibili_api import Credential, user
    credential = Credential(**fields)
    validate_credential(credential)
    try:
        info = await user.get_self_info(credential)
    except Exception as error:
        if getattr(error,"code",None) in (-101,-111):
            raise AssistantError("bili_login_expired","B 站登录已失效，请重新登录。") from None
        raise AssistantError("bili_network","暂时无法向 B 站核验登录。") from None
    finally:
        await close_sdk_session()
    if (not isinstance(info,dict) or type(info.get("mid")) is not int or str(info["mid"])!=str(credential.dedeuserid)
            or not isinstance(info.get("name"),str) or not info["name"].strip()):
        raise AssistantError("bili_identity","无法核对 B 站账号身份。")
    return {"uid":str(info["mid"]),"name":info["name"]}


async def close_sdk_session():
    # 17.4.2 stores one HTTP client per event loop. Release just this owner's
    # cookie jar; a concurrent login check runs on a different loop.
    from bilibili_api.utils.network import get_selected_client, session_pool

    client = session_pool[get_selected_client()[0]].pop(asyncio.get_running_loop(),None)
    if client is not None:
        await client.close()


class DesktopBili:
    login_states = {"starting","waiting","scanned","verified","confirm","browser"}

    def __init__(self, management, folder, *, scan=scan_account, verify=verify_sdk_identity, clock=time.monotonic, browser_verify=verify_identity):
        self.manager, self.scan, self.clock = management, scan, clock
        self.verify, self.browser_verify = verify, browser_verify
        self.path = Path(folder) / "bili-account.sealed"
        self.binding = ("qiban.desktop.bilibili:"+management.installation).encode()
        self.lock = threading.RLock()
        self.ticket = self.session = self.candidate = self.worker = None
        self.state, self.png, self.notice = "idle", None, ""
        self.deadline = 0

    def _account(self):
        try:
            raw = self.path.read_bytes()
            account = json.loads(crypt(raw,self.binding,decrypt=True))
            if not all(isinstance(account[key],str) and account[key] for key in ("name","uid","revision")):
                raise ValueError()
            return account
        except FileNotFoundError:
            return None
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise AssistantError("bili_account_read","无法读取已保存的 B 站账号。") from error

    def _end(self, state, notice):
        self.state, self.notice = state, notice
        self.png = self.candidate = None
        if self.worker:
            loop, task = self.worker
            if not task.done():
                loop.call_soon_threadsafe(task.cancel)
            self.worker = None

    def close(self):
        with self.lock:
            self._end("cancelled","登录已取消。")

    def _paused(self):
        if self.manager.store.state("core-run:bilibili",{}).get("status") == "active":
            raise AssistantError("bili_active","请先停止 B 站角色，再登录或切换账号。")

    def require_run_account(self, accounts):
        with self.lock:
            if self.state in self.login_states and self.clock()<self.deadline:
                raise AssistantError("bili_login_busy","请先完成或取消 B 站登录，再启动角色。")
            current = self._account()
            if not current or accounts != [["bilibili",current["uid"]]]:
                raise AssistantError("account_changed","B 站绑定账号已改变，请重新核对运行账号。")

    def _view(self, account):
        return {"state":self.state, "current":identity(account), "ticket":self.ticket,
                "candidate":identity(self.candidate) if self.state=="confirm" else None,
                "qr":self.png, "message":self.notice}

    async def _work(self, ticket, checking=None):
        def publish(state, png=None):
            with self.lock:
                if self.ticket!=ticket or self.state not in {"starting","waiting","scanned"}:
                    raise asyncio.CancelledError()
                self.state, self.png = state, png
        with self.lock:
            if self.ticket!=ticket or self.state not in {"starting","checking"}:
                return
            self.worker = asyncio.get_running_loop(),asyncio.current_task()
        try:
            if checking is not None:
                async with asyncio.timeout(20):
                    found = await self.verify(checking["cookies"])
                with self.lock:
                    if self.ticket!=ticket or self.state!="checking":
                        return
                    if found["uid"]!=checking["uid"]:
                        raise AssistantError("bili_identity","账号身份不一致。")
                    self.state, self.notice = "checked","已向 B 站核验，当前绑定账号登录有效。"
                return
            async with asyncio.timeout(180):
                verified = await self.scan(publish)
                with self.lock:
                    if self.ticket!=ticket or self.state not in {"starting","waiting","scanned"}:
                        return
                    self.candidate, self.png, self.state = verified, None, "verified"
                # Bound the lifetime of an unconfirmed credential even with the window closed.
                await asyncio.sleep(180)
        except asyncio.CancelledError:
            return
        except Exception as error:
            with self.lock:
                if self.ticket!=ticket or self.state not in self.login_states | {"checking"}:
                    return
                if checking is not None:
                    code = getattr(error,"code","")
                    self._end("failed",{"bili_login_expired":"B 站登录已失效，请重新登录。",
                                        "bili_identity":"无法核对已绑定的 B 站账号，请重新登录。"}.get(
                                            code,"暂时无法向 B 站核验登录，请稍后重试。"))
                    return
                expired = isinstance(error,TimeoutError) or getattr(error,"code","")=="qr_expired"
                messages = {"bili_identity":"B 站返回的账号身份无法核对，请重新扫码。",
                            "login_network_error":"无法连接 B 站登录服务，请检查网络后重试。",
                            "login_timeout":"B 站登录请求超时，请重新扫码。",
                            "login_http_error":"B 站登录服务暂时无法处理请求，请稍后重试。"}
                self._end("expired" if expired else "failed",
                          "本次登录已过期，请重新扫码。" if expired else messages.get(
                              getattr(error,"code",""),"B 站登录或账号验证失败，请稍后重试。"))
        finally:
            with self.lock:
                if self.worker and self.worker[1] is asyncio.current_task():
                    self.worker = None

    def accept_browser(self, session, ticket, cookies):
        names = {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid"}
        if (not isinstance(cookies, dict) or set(cookies) - names or
                any(not isinstance(v, str) or not v or len(v) > 8192 for v in cookies.values()) or
                not {"sessdata", "bili_jct"} <= set(cookies)):
            raise AssistantError("bili_identity", "官方登录未完成，请重新登录。")
        def require_current():
            if not self.manager._active(session):
                raise AssistantError("desktop_locked", "请先解锁本机管理。")
            if (self.session != session or self.ticket != ticket or self.state != "browser" or
                    self.clock() >= self.deadline):
                raise AssistantError("bili_stale", "登录请求已过期或取消，请重新登录。")
            self._paused()
        with self.manager.store.lock, self.lock:
            require_current()
        try:
            found = asyncio.run(self.browser_verify(cookies))
        except Exception:
            raise AssistantError("bili_identity", "无法核验官方登录，请重试。") from None
        with self.manager.store.lock, self.lock:
            require_current()
            self.candidate = {**found, "cookies": {**cookies, "dedeuserid": found["uid"]}}
            self.state = "verified"
            return self.operate("status", session)

    def operate(self, operation, session, ticket=None):
        with self.manager.store.lock, self.lock:
            if not self.manager._active(session):
                raise AssistantError("desktop_locked","请先解锁本机管理。")
            if self.session!=session:
                self._end("idle","")
                self.ticket, self.session = None, session
            account = self._account()
            revision = account["revision"] if account else None
            if self.state=="checked" and self.original!=revision:
                self._end("idle","")
            pending = self.state in self.login_states | {"checking"}
            if pending and self.clock()>=self.deadline:
                self._end("expired","本次登录已过期，请重新扫码。")
                pending = False
            if operation == "browser":
                self._paused()
                if pending:
                    raise AssistantError("bili_login_busy", "请先完成或取消当前登录。")
                self.ticket, self.original = uuid.uuid4().hex, revision
                self.state, self.notice, self.deadline = "browser", "请在官方登录窗口选择手机验证码或账号密码登录。", self.clock()+300
                return self._view(account)
            if operation in {"check","login"}:
                if operation=="login":
                    self._paused()
                elif not account:
                    raise AssistantError("bili_unbound","请先绑定 B 站账号。")
                if not pending:
                    self.ticket, self.original = uuid.uuid4().hex, revision
                    self.state = "checking" if operation=="check" else "starting"
                    self.notice, self.deadline = "", self.clock()+180
                    threading.Thread(target=asyncio.run,args=(self._work(
                        self.ticket,account if operation=="check" else None),),daemon=True).start()
                return self._view(account)
            if operation in {"cancel","confirm"} and (not self.ticket or ticket!=self.ticket):
                raise AssistantError("bili_stale","登录请求已变化，请重新查看账号。")
            if operation=="cancel":
                if pending:
                    self._end("cancelled","核验已取消。" if self.state=="checking" else "登录已取消。")
                return self._view(account)
            if pending and self.original!=revision:
                self._end("failed","已绑定账号发生变化，请重新登录。")
            if self.state=="verified" and account and self.candidate["uid"]!=account["uid"]:
                self.state = "confirm"
                self.notice = "登录账号与当前绑定不同，请核对后选择。"
                return self._view(account)
            if self.state=="verified" or (self.state=="confirm" and operation=="confirm"):
                self._paused()
                saved = {**self.candidate,"revision":uuid.uuid4().hex}
                try:
                    with FileLock(str(self.path)+".lock",timeout=0):
                        latest = self._account()
                        if (latest["revision"] if latest else None)!=self.original:
                            self._end("failed","已绑定账号发生变化，请重新登录。")
                            return self._view(latest)
                        atomic_write(self.path,crypt(json.dumps(saved,ensure_ascii=False).encode(),self.binding))
                except (Timeout,OSError):
                    raise AssistantError("bili_account_write","保存账号失败，请重试。") from None
                account = saved
                self._end("saved","B 站账号已保存。")
            return self._view(account)
