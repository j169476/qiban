"""Offline first-group recovery uses actual activation, even on first sight of that group."""

from copy import deepcopy

import pytest

from companion_core import reply_audience
from companion_core.reply_audience import Audience, LiveMembers, registry
from companion_qq.native_receiver import NativeReceiver
from tests.test_qq_native_normalization import IDENTITY
from tests_postgres.test_desktop_management import desktop_store, service
from tests_postgres.test_desktop_management import desktop_store as pg_store
from tests_postgres.test_qq_module import STAMP, env
from tests_postgres.test_qq_native_windows import bound, message
from tests_postgres.test_reply_audiences import set_mode

__all__ = ["pg_store", "desktop_store", "env", "service"]


@pytest.mark.asyncio
async def test_offline_new_group_replays_once_from_enable_not_discovery(env, monkeypatch):
    store = env.store
    category = Audience("qq", env.module.account, "group")
    monkeypatch.setattr(reply_audience.time, "time", lambda: STAMP - 1)
    policy = set_mode(store, category, True)
    assert policy["since"] == STAMP - 1
    live = registry(store)[category] = LiveMembers()
    live.replace(policy["version"], {"10003": "10003"})

    class Window:
        def request(self, command, **args):
            if command == "group_head":
                return bound(3)
            if command == "group_open":
                assert args["since"] == STAMP - 1 and args["lower"] is None
                return {
                    "window_id": "first",
                    "upper": bound(3),
                    "count": 2,
                    "history_status": {"status": "complete", "reasons": []},
                }
            assert command == "group_page"
            return {
                "window_id": "first",
                "kind": "group",
                "target": "10003",
                "page": 0,
                "complete": True,
                "lower": None,
                "upper": bound(3),
                "messages": [message(2, mention=True), message(3)],
                "history_status": {"status": "complete", "reasons": []},
            }

    receiver = NativeReceiver(env.module, Window(), deepcopy(IDENTITY), guard=lambda: None)
    assert receiver.poll("group", "10003")["status"] == "staged"
    assert receiver.poll("group", "10003")["status"] == "published"
    events = await env.module.runtime_poll()
    assert len(events) == 1 and env.module.decision_batch(events[0])["catchup"]
    await env.core.handle(events[0])
    assert len(env.provider.sent) == 1
    assert (await env.module.runtime_poll()) == []
    assert receiver.poll("group", "10003")["status"] == "idle"


def test_reenabled_category_uses_new_cutoff_and_same_mode_retry_keeps_original(desktop_store, monkeypatch):
    cat = Audience("wechat", "login", "private")
    monkeypatch.setattr(reply_audience.time, "time", lambda: 1000.25)
    first = set_mode(desktop_store, cat, True)
    assert first["since"] == 1001
    monkeypatch.setattr(reply_audience.time, "time", lambda: 2000)
    assert set_mode(desktop_store, cat, True) == first
    set_mode(desktop_store, cat, False)
    second = set_mode(desktop_store, cat, True)
    assert second["version"] != first["version"] and second["since"] == 2000


def test_desktop_preview_does_not_enable_and_save_is_bound_to_account(service):
    from qiban.desktop_qq import KEY
    from tests_postgres.test_desktop_management import call

    def selected(account):
        service.store.set_state(
            KEY, {"revision": 1, "selected": {"uin": account}, "configurations": {account: {}}}
        )

    selected("12345")
    args = {"platform": "qq", "account": "12345", "kind": "group", "enabled": True}
    cat = Audience("qq", "12345", "group")
    first = call(service, "audience.preview", args)["result"]
    assert "高风险" in first["message"] and not reply_audience.read(service.store, cat)["enabled"]
    selected("54321")
    assert call(service, "audience.save", {"token": first["token"]})["error"] == "edit_conflict"
    selected("12345")
    second = call(service, "audience.preview", args)["result"]
    result = call(service, "audience.save", {"token": second["token"]})
    assert result["result"]["since"] > 0 and result["result"]["enabled"]
    assert not reply_audience.read(service.store, Audience("qq", "12345", "private"))["enabled"]
    assert call(service, "audience.save", {"token": second["token"]})["error"] == "edit_conflict"

@pytest.mark.asyncio
async def test_runtime_observes_unlisted_group_and_refuses_removed_member(desktop_store, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from companion_core.runtime import Companion
    from companion_qq.config import native_parameters
    from companion_qq.module import QQModule
    from companion_qq.native_provider import NativeProvider
    from tests_postgres.test_qq_native_provider import NativeWorker
    from tests_postgres.test_wechat_role import Host, Model
    store=desktop_store
    cat=Audience("qq","10001","group")
    monkeypatch.setattr(reply_audience.time,"time",lambda:STAMP-1)
    set_mode(store,cat,True)
    options={"version":2,"transport":"native","account":"10001","uid":"u_self","private_targets":[],"groups":[],
             "runtime":str(tmp_path/"runtime"),"data":str(tmp_path/"data")}
    class DynamicWorker(NativeWorker):
        def __init__(self,config):
            super().__init__(config)
            assert config["follow"]=={"group":True}
            self.identity["tenures"]={"group":{"10003":1}}
            self.upper=bound(3)
        def request(self,command,**args):
            if command=="group_open":
                assert args["since"]==STAMP-1
                return {"window_id":"offline","upper":self.upper,"count":2,"history_status":{"status":"complete","reasons":[]}}
            if command=="group_page":
                return {"window_id":"offline","page":0,"kind":"group","target":"10003","lower":None,"upper":self.upper,
                        "complete":True,"messages":[message(2,mention=True),message(3)],"history_status":{"status":"complete","reasons":[]}}
            return super().request(command,**args)
    module=QQModule(store,tmp_path,options=options,factory=lambda:NativeProvider(
        store,native_parameters(options),worker_factory=DynamicWorker,follow_settings=True))
    module.probe()
    core=Companion(store,Model(),Host(),[module],clock=lambda:datetime.fromtimestamp(STAMP,timezone.utc))
    core.start()
    try:
        assert not await module.runtime_poll()
        pending=await module.runtime_poll()
        assert len(pending)==1 and module.decision_batch(pending[0])["catchup"]
        provider=module.provider
        provider.worker.identity["groups"]=[]
        provider.worker.identity["tenures"]["group"]={}
        provider.identity()
        from companion_core.errors import AssistantError
        with pytest.raises(AssistantError):
            await core.handle(pending[0])
        assert provider.worker.submits==0
    finally:
        core.stop()
        await module.close()
