"""Account broker contract tests: no real credentials, model turns, or network."""
import asyncio
import json
from pathlib import Path

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_codex import DesktopCodex, login_url


class Client:
    account=None
    origin='https://auth.openai.com/authorize?synthetic=1'
    fail=False
    hold=False

    async def open(self, command, **options):
        self.command,self.options=command,options
        self.calls=[]
        self.closed=False
        self.home=options['env']['CODEX_HOME']
        return self

    async def call(self, method, params, on_event=lambda row:None):
        self.calls.append((method,params))
        if self.fail:
            raise ValueError('SYNTHETIC-SECRET-MUST-NOT-LEAK')
        if method=='initialize':
            return {'codexHome':self.home}
        if method=='account/read':
            return {'account':self.account}
        if method=='account/logout':
            self.account=None
            return {}
        if method=='account/login/start':
            return {'loginId':'synthetic-login','authUrl':self.origin}
        raise AssertionError(method)

    def send(self, value):
        assert value['method']=='initialized'

    def reject_request(self, row):
        pass

    async def receive(self):
        while self.hold:
            self.options['guard']()
            await asyncio.sleep(.01)
        self.account={'type':'chatgpt','email':'synthetic@example.invalid','planType':'pro'}
        return {'method':'account/login/completed','params':{'loginId':'synthetic-login','success':True}}

    def close(self):
        self.closed=True


def broker(tmp_path, client):
    return DesktopCodex(tmp_path/'application-account',locate=lambda:tmp_path/'codex.exe',rpc=client)


def finish(value, operation):
    value.begin(operation)
    value.thread.join(3)
    assert not value.thread.is_alive()
    return value.status()


def test_login_uses_dedicated_home_and_never_starts_turn(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-global-api-key')
    monkeypatch.setenv('CODEX_THREAD_ID','synthetic-global-chat')
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'global-codex'))
    client=Client()
    value=broker(tmp_path,client)
    result=finish(value,'login')
    assert result['state']=='authenticated' and result['model']=='gpt-6.1-terra'
    assert result['model_calls_enabled'] is False
    assert client.closed
    env=client.options['env']
    assert 'OPENAI_API_KEY' not in env and 'CODEX_THREAD_ID' not in env
    assert Path(env['CODEX_HOME'])==value.root/'home'
    config=(value.root/'home/config.toml').read_text()
    assert 'cli_auth_credentials_store="keyring"' in config and 'shell_tool=false' in config
    assert not (value.root/'home/auth.json').exists()
    assert not any(method.startswith(('thread/','turn/')) for method,_ in client.calls)
    assert 'accessToken' not in json.dumps(result)


def test_refresh_logout_and_restart_do_not_copy_global_credentials(tmp_path):
    client=Client()
    client.account={'type':'chatgpt','email':'test@example.invalid','planType':'plus'}
    first=broker(tmp_path,client)
    assert finish(first,'refresh')['state']=='authenticated'
    assert ('account/read',{'refreshToken':True}) in client.calls
    assert finish(first,'logout')['state']=='signed_out'
    assert ('account/logout',{}) in client.calls
    restarted=broker(tmp_path,client)
    assert finish(restarted,'refresh')['state']=='signed_out'


def test_cancel_stops_owned_login_and_clears_browser_link(tmp_path):
    import time

    client=Client()
    client.hold=True
    value=broker(tmp_path,client)
    value.begin('login')
    deadline=time.monotonic()+3
    while value.status()['state']!='waiting' and time.monotonic()<deadline:
        time.sleep(.01)
    assert value.open_login()['url']==client.origin
    with pytest.raises(AssistantError,match='仍在进行'):
        value.begin('logout')
    assert value.cancel()=={'cancel_requested':True}
    value.thread.join(3)
    assert not value.thread.is_alive() and client.closed
    assert value.status()['state']=='cancelled' and not value.status()['can_open_login']
    with pytest.raises(AssistantError):
        value.open_login()
    assert value.cancel()=={'cancel_requested':False}


def test_cancel_does_not_claim_success_when_account_operation_finished(tmp_path):
    client=Client()
    client.account={'type':'chatgpt','email':'test@example.invalid','planType':'plus'}
    value=broker(tmp_path,client)
    assert finish(value,'refresh')['state']=='authenticated'
    assert value.cancel()=={'cancel_requested':False}
    assert value.status()['state']=='authenticated'


@pytest.mark.parametrize('url',['http://auth.openai.com/a','https://auth.openai.com.evil.invalid/a',
    'file:///C:/a','https://user@auth.openai.com/a','https://chatgpt.com:444/a'])
def test_login_rejects_untrusted_origin(url):
    with pytest.raises(AssistantError):
        login_url(url)


def test_auth_failures_do_not_expose_raw_exception(tmp_path):
    client=Client()
    client.fail=True
    value=broker(tmp_path,client)
    result=finish(value,'refresh')
    assert result['state']=='error' and client.closed
    assert 'SYNTHETIC-SECRET' not in json.dumps(result)


def test_api_key_auth_is_not_accepted_as_subscription(tmp_path):
    client=Client()
    client.account={'type':'apiKey'}
    value=broker(tmp_path,client)
    assert finish(value,'refresh')['code']=='model_auth_type'


def test_closed_broker_cannot_restart(tmp_path):
    value=broker(tmp_path,Client())
    value.close()
    with pytest.raises(AssistantError):
        value.begin('login')
