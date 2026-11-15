"""Desktop login uses synthetic platform responses and Windows user encryption."""
import asyncio
import json
import time

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_bili import DesktopBili
from tests_postgres.test_desktop_management import call, desktop_store, service

__all__ = ["desktop_store", "service"]


@pytest.fixture
def login(service,tmp_path):
    now = [10]
    accounts = []
    async def scan(publish):
        publish("waiting","synthetic-qr")
        await asyncio.sleep(.01)
        item = accounts.pop(0)
        return {**item, "cookies":{"sessdata":"synthetic-secret", "bili_jct":"test-csrf", "dedeuserid":item["uid"]}}
    broker = DesktopBili(service,tmp_path,scan=scan,clock=lambda:now[0])
    service.bili = broker
    def op(name, **extra):
        return call(service,"bili."+name,{"session":service.session_id,**extra})
    def start(uid,name):
        accounts.append({"uid":uid,"name":name})
        result = op("login")["result"]
        for _ in range(100):
            result = op("status")["result"]
            if result["state"] in {"confirm","saved"}:
                return result
            time.sleep(.015)
        pytest.fail("Synthetic scan did not reach account verification")
    try:
        yield broker, op, start, now
    finally:
        broker.close()


def test_first_renew_cancel_switch_and_restart(login,service,tmp_path):
    broker,op,start,_ = login
    assert op("status")["result"]["current"] is None
    assert start("101","原账号")["state"] == "saved"
    assert start("101","原账号新昵称")["state"] == "saved"
    old = broker.path.read_bytes()
    pending = start("202","另一个昵称🙂")
    assert pending["state"] == "confirm" and pending["qr"] is None
    assert pending["current"] == {"uid":"101","name":"原账号新昵称"}
    assert pending["candidate"] == {"uid":"202","name":"另一个昵称🙂"}
    assert "synthetic-secret" not in json.dumps(pending)
    assert op("cancel",ticket=pending["ticket"])["result"]["state"] == "cancelled"
    assert broker.path.read_bytes() == old
    next_try = start("202","新账号")
    assert op("confirm",ticket=pending["ticket"])["error"] == "bili_stale"
    assert broker.path.read_bytes() == old
    saved = op("confirm",ticket=next_try["ticket"])["result"]
    assert saved["current"] == next_try["candidate"] and saved["state"] == "saved"
    ciphertext = broker.path.read_bytes()
    assert ciphertext != old and b"synthetic-secret" not in ciphertext
    assert op("confirm",ticket=next_try["ticket"])["result"] == saved
    assert broker.path.read_bytes() == ciphertext
    restarted = DesktopBili(service,tmp_path)
    assert restarted.operate("status",service.session_id)["current"] == saved["current"]


def test_expiry_lock_and_write_failure_preserve_binding(login,service,monkeypatch):
    broker,op,start,now = login
    assert call(service,"lock.enable",{"password":"temporary-strong-pass"})["ok"]
    start("101","原账号")
    old = broker.path.read_bytes()
    pending = start("202","新账号")
    now[0] += 181
    assert op("confirm",ticket=pending["ticket"])["result"]["state"] == "expired"
    assert broker.path.read_bytes() == old
    pending = start("202","新账号")
    session = service.session_id
    assert call(service,"session.lock")["ok"]
    assert call(service,"session.unlock",{"password":"temporary-strong-pass"})["ok"]
    assert call(service,"bili.confirm",{"session":session,"ticket":pending["ticket"]})["error"] == "desktop_locked"
    assert op("confirm",ticket=pending["ticket"])["error"] == "bili_stale"
    pending = start("202","新账号")
    def denied(*args):
        raise OSError("test")
    monkeypatch.setattr("qiban.desktop_bili.atomic_write",denied)
    assert op("confirm",ticket=pending["ticket"])["error"] == "bili_account_write"
    assert broker.path.read_bytes() == old


def test_binding_revision_and_running_role_checked_at_confirmation(login,service):
    broker,op,start,_ = login
    start("101","原账号")
    pending = start("202","新账号")
    service.store.set_state("core-run:bilibili",{"status":"active"})
    assert op("confirm",ticket=pending["ticket"])["error"] == "bili_active"
    assert broker._account()["uid"]=="101"
    service.store.set_state("core-run:bilibili",{"status":"stopped"})
    # Simulate another worker completing a renewal while this confirmation is open.
    from bili_assistant.browser_login_transport import crypt
    from companion_core.storage import atomic_write
    value = {**broker._account(),"revision":"other-worker"}
    atomic_write(broker.path,crypt(json.dumps(value).encode(),broker.binding))
    result = op("confirm",ticket=pending["ticket"])["result"]
    assert result["state"]=="failed" and result["current"]["uid"]=="101"
    assert broker._account()["revision"]=="other-worker"


async def test_live_policy_applies_to_existing_executor_and_preserves_ledger(login,service):
    from copy import deepcopy

    from bili_assistant.connector import BilibiliConnector
    from bili_assistant.engine import Engine
    from qiban.desktop_runtime import DesktopRuntime
    from tests_postgres.test_runtime import Host

    _,op,start,_=login
    start("101","账号一")
    def load():
        return op("policy",uid="101")["result"]
    def save(settings):
        return op("policy_save",uid="101",expected=settings["revision"],actions=settings["actions"])

    defaults=load()
    assert {k:v["daily_limit"] for k,v in defaults["actions"].items()}=={
        "comment":2,"reply":5,"comment_like":5,"dm_reply":3,"dynamic":1,"like":10,"favorite":10,"follow":2,"coin":2,
        "favorite_create":5,"favorite_move":50}
    assert all(rule["enabled"] for rule in defaults["actions"].values())
    sent=[]
    class Platform:
        async def me(self):
            return {"uid":"101"}
        async def perform(self,kind,payload,policy,before_send):
            if payload["text"]=="发送前变更":
                def change():
                    edit=load()
                    assert edit["used"]["dynamic"]==1
                    edit["actions"]["dynamic"]["enabled"]=False
                    assert save(edit)["ok"]
                await asyncio.wait_for(asyncio.to_thread(change),5)
            before_send()
            sent.append(payload["text"])
            return {"dynamic_id":"123"}
    adapter=BilibiliConnector(Engine(service.store,Host(),Platform()),account="101")
    runtime=DesktopRuntime(service,"bili-policy-test",model=object(),adapters=[adapter])
    try:
        run=runtime.start("bilibili")["run"]
        engine=adapter.engine
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        clock=[datetime.now(ZoneInfo(defaults["timezone"])).replace(hour=12,minute=0,second=0)]
        engine.clock=lambda:clock[0]
        assert engine.store.read("manifest")["data"]["account_uid"]=="101"
        assert service.store.read("manifest")["data"]["account_uid"]!="101"
        async def execute(text):
            action=engine.prepare("dynamic",{"text":text})
            return await engine.execute(action["id"])
        assert (await execute("发送前变更"))["status"]=="cancelled"
        assert not sent and load()["used"]["dynamic"]==0
        with pytest.raises(AssistantError) as denied:
            await execute("已禁用")
        assert denied.value.code=="action_disabled"
        edit=load()
        edit["actions"]["dynamic"]["enabled"]=True
        assert save(edit)["ok"]
        clock[0]+=timedelta(minutes=6)
        assert (await execute("一次动态"))["status"]=="succeeded"
        edit=load()
        assert edit["used"]["dynamic"]==1
        edit["actions"]["dynamic"]["daily_limit"]=0
        assert save(edit)["result"]["used"]["dynamic"]==1
        with pytest.raises(AssistantError) as exhausted:
            await execute("调低后")
        assert exhausted.value.code=="quota_exhausted"
        assert sent==["一次动态"]
        assert runtime.cores["bilibili"].guard(run["generation"])
        stale=deepcopy(defaults)
        stale["actions"]["coin"]["daily_limit"]=17
        assert save(stale)["error"]=="edit_conflict"
        runtime.control(service.request(),"bilibili","stop")
        pending=start("202","账号二")
        assert op("confirm",ticket=pending["ticket"])["ok"]
        assert save(edit)["error"]=="account_changed"
        assert op("policy",uid="202")["result"]["actions"]==defaults["actions"]
        pending=start("101","账号一")
        op("confirm",ticket=pending["ticket"])
        assert load()["actions"]["dynamic"]["daily_limit"]==0
        assert load()["used"]["dynamic"]==1
    finally:
        runtime.close()


def test_action_units_stay_on_reserved_day_and_do_not_transfer_to_other_accounts(login,service):
    from datetime import date, timedelta
    _,op,start,_=login
    start("101","配额日期验证")
    before=op("policy",uid="101")["result"]
    today=before["day"]
    yesterday=(date.fromisoformat(today)-timedelta(days=1)).isoformat()
    with service.store.db() as db:
        for i,(uid,day,status,units) in enumerate([
            ("101",today,"dispatching",2),("101",today,"succeeded",1),("101",today,"uncertain",2),
            ("101",today,"failed",2),("101",today,"cancelled",2),("101",yesterday,"succeeded",2),
            ("202",today,"succeeded",2)]):
            db.execute("INSERT INTO actions(id,dedupe,owner,generation,account,kind,payload,status,day,units,created) "
                       "VALUES(?,?,?,?,?,'coin',?,?,?,?,?)",
                       (str(i),str(i),"fixture","fixture",uid,service.store.ledger_encode("actions.payload",{}),
                        status,day,units,today))
    rules=before["actions"]
    assert op("policy",uid="101")["result"]["used"]["coin"]==5
    rules["coin"]["daily_limit"]=8
    result=op("policy_save",uid="101",expected=before["revision"],actions=rules)["result"]
    assert result["actions"]["coin"]["daily_limit"]-result["used"]["coin"]==3
    rules["coin"]["daily_limit"]=True
    assert op("policy_save",uid="101",expected=result["revision"],actions=rules)["error"]=="invalid_policy"
    assert op("policy",uid="101")["result"]["revision"]==result["revision"]
    restored=op("policy_save",uid="101",expected=result["revision"],actions=result["defaults"])["result"]
    assert restored["actions"]["coin"]["daily_limit"]==2 and restored["used"]["coin"]==5
    assert service.store.state("core-run:bilibili",{}).get("status")!="active"


def test_online_check_preserves_credentials_and_distinguishes_expiry_from_network(login):
    broker,op,start,_ = login
    assert op("check")["error"]=="bili_unbound"
    start("101","原账号")
    sealed=broker.path.read_bytes()
    for outcome,expected in [("101","登录有效"),("202","无法核对"),("expired","登录已失效"),("offline","稍后重试")]:
        async def verify(fields):
            assert fields["dedeuserid"]=="101"
            if outcome=="expired":
                raise AssistantError("bili_login_expired","test")
            if outcome=="offline":
                raise OSError("network")
            return {"uid":outcome,"name":"平台昵称"}
        broker.verify=verify
        assert op("check")["result"]["state"]=="checking"
        for _ in range(100):
            value=op("status")["result"]
            if value["state"]!="checking":
                break
            time.sleep(.01)
        assert expected in value["message"] and value["current"]["uid"]=="101"
        assert value["qr"] is None and broker.path.read_bytes()==sealed


def test_bili_role_start_stop_and_account_replacement(login,service,monkeypatch):
    from qiban.desktop_runtime import DesktopRuntime
    from tests_postgres.test_desktop_runtime import Adapter

    broker,op,start,_ = login
    start("101","原账号")
    adapter=Adapter()
    adapter.platform,adapter.account="bilibili","101"
    runtime=DesktopRuntime(service,"bili-run-test",model=object(),adapters=[adapter])
    try:
        assert not runtime.reconcile()["bilibili"]["started"]
        pending=start("202","新账号")
        with pytest.raises(AssistantError) as busy:
            runtime.start("bilibili")
        assert busy.value.code=="bili_login_busy"
        op("cancel",ticket=pending["ticket"])
        run=runtime.start("bilibili")["run"]
        assert runtime.cores["bilibili"].guard(run["generation"])["accounts"]==[["bilibili","101"]]
        with monkeypatch.context() as patch:
            patch.setattr(broker,"_account",lambda:{"uid":"202"})
            with pytest.raises(AssistantError) as stale:
                runtime.cores["bilibili"].guard(run["generation"])
            assert stale.value.code=="account_changed"
        assert op("login")["error"]=="bili_active"
        runtime.control(service.request(),"bilibili","stop")
        old_core=runtime.cores["bilibili"]
        pending=start("202","新账号")
        assert op("confirm",ticket=pending["ticket"])["result"]["state"]=="saved"
        with pytest.raises(AssistantError) as changed:
            old_core.start()
        assert changed.value.code=="account_changed" and adapter.stopped==[run["generation"]]
        assert runtime.cores["bilibili"] is not old_core and not runtime.reconcile()["bilibili"]["started"]
        assert runtime.start("bilibili")["run"]["accounts"]==[["bilibili","202"]]
    finally:
        runtime.close()


def test_official_browser_login_verifies_identity_and_requires_account_switch_confirmation(login):
    broker, op, start, now = login
    start("101", "Original")
    previous = broker.path.read_bytes()
    seen = []
    async def verify(cookies):
        seen.append(cookies)
        return {"uid": "202", "name": "Official browser"}
    broker.browser_verify = verify
    browser = op("browser")["result"]
    assert browser["state"] == "browser" and not browser["qr"]
    cookies = {"sessdata": "synthetic-browser-session", "bili_jct": "synthetic-csrf"}
    reply = op("browser_complete", ticket=browser["ticket"], cookies=cookies)
    assert reply["ok"] and reply["result"]["state"] == "confirm"
    assert seen == [cookies] and broker.path.read_bytes() == previous
    assert "synthetic-browser-session" not in json.dumps(reply)
    assert op("confirm", ticket=browser["ticket"])["result"]["current"]["uid"] == "202"
    assert b"synthetic-browser-session" not in broker.path.read_bytes()


def test_browser_login_cancel_expiry_and_lock_during_verification_do_not_save(login, service):
    broker, op, start, now = login
    start("101", "Original")
    previous = broker.path.read_bytes()
    cookies = {"sessdata": "synthetic-browser-session", "bili_jct": "synthetic-csrf"}
    async def verify(fields):
        return {"uid": "202", "name": "New"}
    broker.browser_verify = verify
    browser = op("browser")["result"]
    op("cancel", ticket=browser["ticket"])
    assert op("browser_complete", ticket=browser["ticket"], cookies=cookies)["error"] == "bili_stale"
    browser = op("browser")["result"]
    now[0] += 301
    assert op("browser_complete", ticket=browser["ticket"], cookies=cookies)["error"] == "bili_stale"
    op("status")
    browser = op("browser")["result"]
    assert op("browser_complete", ticket=browser["ticket"], cookies={**cookies, "password": "synthetic"})["error"] == "bili_identity"
    assert call(service, "lock.enable", {"password": "temporary-strong-pass"})["ok"]
    browser = op("browser")["result"]
    async def lock_during_verify(fields):
        assert call(service, "session.lock")["ok"]
        return {"uid": "202", "name": "New"}
    broker.browser_verify = lock_during_verify
    assert op("browser_complete", ticket=browser["ticket"], cookies=cookies)["error"] == "desktop_locked"
    assert broker.path.read_bytes() == previous
