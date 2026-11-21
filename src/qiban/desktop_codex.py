"""Application-owned managed ChatGPT login. This broker never starts a model turn."""
from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from companion_codex import __version__
from companion_codex.app_server import RpcProcess
from companion_codex.isolation import DISABLED_FEATURES
from companion_core.errors import AssistantError


def login_url(value):
    if not isinstance(value, str) or len(value)>16000:
        raise AssistantError('model_login_origin','登录地址无效，请重新登录')
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or parsed.hostname not in {'auth.openai.com','chatgpt.com'}
            or parsed.username or parsed.password or parsed.port not in (None,443)):
        raise AssistantError('model_login_origin','登录地址无效，请重新登录')
    return value


def executable():
    found=shutil.which('codex.exe')
    if found:
        return Path(found).resolve(strict=True)
    local=os.environ.get('LOCALAPPDATA')
    candidates=list((Path(local)/'OpenAI/Codex/bin').glob('*/codex.exe')) if local else []
    if not candidates:
        raise AssistantError('model_unavailable','未找到 Codex，请安装后重新检查')
    return max(candidates,key=lambda path:path.stat().st_mtime_ns).resolve(strict=True)


class DesktopCodex:
    """One bounded account operation at a time; only public account data leaves it."""
    working_message = '正在连接 Codex……'
    operations = ('refresh','login','logout')
    def __init__(self, root, *, locate=executable, rpc=RpcProcess):
        self.root=Path(root).resolve()
        self.locate,self.rpc=locate,rpc
        self.lock=threading.RLock()
        self.cancelled=threading.Event()
        self.thread=None
        self.closed=False
        self.state={'state':'unchecked','message':'尚未检查模型登录状态。'}
        self.url=None

    def status(self):
        with self.lock:
            return {**self.state,'busy':bool(self.thread and self.thread.is_alive()),
                    'can_open_login':bool(self.url),'model':'gpt-6.1-terra','effort':'low',
                    'credential_store':'windows','model_calls_enabled':False}

    def _set(self, **values):
        with self.lock:
            self.state=values

    def begin(self, operation):
        if operation not in self.operations:
            raise AssistantError('model_operation','模型账号操作无效')
        with self.lock:
            if self.closed:
                raise AssistantError('model_closed','模型登录服务已关闭')
            if self.thread and self.thread.is_alive():
                raise AssistantError('model_busy','账号操作仍在进行，请完成或取消后再试')
            self.cancelled.clear()
            self.url=None
            self.state={'state':'working','message':self.working_message}
            self.thread=threading.Thread(target=self._run,args=(operation,),daemon=True)
            self.thread.start()
        return self.status()

    def cancel(self):
        with self.lock:
            requested=bool(self.thread and self.thread.is_alive())
            if requested:
                self.cancelled.set()
            self.url=None
        return {'cancel_requested':requested}

    def open_login(self):
        with self.lock:
            if self.closed or self.cancelled.is_set() or not self.url or self.state['state']!='waiting':
                raise AssistantError('model_login_expired','当前没有待完成的登录，请重新发起')
            return {'url':login_url(self.url)}

    def close(self):
        self.closed=True
        self.cancel()
        if self.thread:
            self.thread.join(timeout=5)

    def _guard(self):
        if self.closed or self.cancelled.is_set():
            raise AssistantError('model_cancelled','已取消账号操作；请重新检查当前登录状态。')

    def _environment(self, exe):
        home,work,temp=(self.root/name for name in ('home','work','temp'))
        for path in (home,work,temp):
            path.mkdir(parents=True,exist_ok=True)
        # Dedicated configuration and OS vault namespace; never import the user's global login.
        config=['cli_auth_credentials_store="keyring"','project_doc_max_bytes=0',
                'approval_policy="never"','sandbox_mode="read-only"','web_search="disabled"',
                'model="gpt-6.1-terra"','model_reasoning_effort="low"','[analytics]','enabled=false',
                '[features]',*(name+'=false' for name in DISABLED_FEATURES)]
        (home/'config.toml').write_text('\n'.join(config)+'\n',encoding='utf8')
        source={key.upper():value for key,value in os.environ.items()}
        env={key:source[key] for key in ('SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT') if key in source}
        env.update(CODEX_HOME=str(home),HOME=str(home),USERPROFILE=str(home),
                   APPDATA=str(home/'AppData/Roaming'),LOCALAPPDATA=str(home/'AppData/Local'),
                   TEMP=str(temp),TMP=str(temp),PATH=str(exe.parent))
        return home,work,env

    def _run(self, operation):
        try:
            asyncio.run(self._account(operation))
        except AssistantError as error:
            messages={'model_cancelled':'已取消账号操作；请重新检查当前登录状态。',
                      'model_unavailable':'未找到 Codex，请安装后重新检查。',
                      'model_timeout':'账号操作超时，请重新检查网络和登录状态。'}
            self._set(state='cancelled' if error.code=='model_cancelled' else 'error',
                      code=error.code,message=messages.get(error.code,'模型账号操作未完成，请检查网络后重试。'))
        except Exception:
            self._set(state='error',code='model_account_failed',message='模型账号操作未完成，请检查网络后重试。')
        finally:
            with self.lock:
                self.url=None

    def _public_account(self, response):
        account=response.get('account')
        if account is None:
            self._set(state='signed_out',message='尚未登录，请使用 ChatGPT 账号登录。')
        elif account.get('type')=='chatgpt':
            self._set(state='authenticated',message='ChatGPT 账号已登录。',
                      email=account.get('email',''),plan=account.get('planType','unknown'))
        else:
            raise AssistantError('model_auth_type','当前登录不是 ChatGPT 订阅账号')

    async def _account(self, operation):
        self._guard()
        exe=self.locate()
        home,work,env=self._environment(exe)
        client=await self.rpc.open([str(exe),'app-server','--stdio','--strict-config'],
            cwd=work,env=env,guard=self._guard,deadline=time.monotonic()+(600 if operation=='login' else 30))
        try:
            initialized=await client.call('initialize',{'clientInfo':{'name':'qiban_account','version':__version__}})
            if Path(initialized.get('codexHome','')).resolve()!=home:
                raise AssistantError('model_account_scope','Codex 登录目录不匹配')
            client.send({'method':'initialized','params':{}})
            if operation=='logout':
                await client.call('account/logout',{})
            if operation=='login':
                existing=await client.call('account/read',{'refreshToken':False})
                if existing.get('account') is not None:
                    self._public_account(existing)
                    return
                completed=[]
                def event(row):
                    if row.get('method')=='account/login/completed':
                        completed.append(row.get('params',{}))
                pending=await client.call('account/login/start',{'type':'chatgpt','useHostedLoginSuccessPage':True},event)
                url=login_url(pending.get('authUrl'))
                with self.lock:
                    self._guard()
                    self.url=url
                    self.state={'state':'waiting','message':'请在官方登录页面完成 ChatGPT 账号授权。'}
                while True:
                    self._guard()
                    matching=next((item for item in completed if item.get('loginId')==pending.get('loginId')),None)
                    if matching is not None:
                        if matching.get('success') is not True:
                            raise AssistantError('model_login_failed','账号授权未完成')
                        break
                    row=await client.receive()
                    client.reject_request(row)
                    event(row)
            self._guard()
            self._public_account(await client.call('account/read',{'refreshToken':operation=='refresh'}))
        finally:
            await asyncio.to_thread(client.close)
