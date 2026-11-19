"""First enrollment is installed for one account and never grants reply permission."""
import hashlib
import os
from pathlib import Path

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations
from qiban.desktop_runtime import DesktopRuntime
from qiban.desktop_wechat import adapter, register_source
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_wechat.test_receiver import snapshot

__all__ = ["desktop_store", "service"]


@pytest.mark.parametrize("worker_result", ["empty", "unsupported"])
async def test_unknown_running_version_is_visible_on_account_check(tmp_path, monkeypatch, worker_result):
    from companion_wechat import receiver_transport
    from companion_wechat.providers import uia
    from qiban.desktop_wechat import detect_accounts, version_status

    monkeypatch.setattr(uia, "running_client_versions", lambda: ["99.0.0.1"])
    assert version_status()["unrecognized_versions"] == ["99.0.0.1"]
    (tmp_path / "Qiban.WeChatReader.exe").touch()
    class Reader:
        def __init__(self, **kwargs):
            pass

        async def request(self, command):
            if worker_result == "unsupported":
                raise receiver_transport.WorkerError("client_version_unsupported")
            return {"accounts": []}

        async def close(self):
            pass

    monkeypatch.setattr(receiver_transport, "SubprocessTransport", Reader)
    with pytest.raises(AssistantError, match="99.0.0.1") as failure:
        await detect_accounts(tmp_path)
    assert failure.value.code == "client_version_unsupported"


def test_directory_search_preserves_account_revision_and_rejects_late_pages(service, tmp_path, monkeypatch):
    from companion_wechat import receiver_transport

    service.wechat_resources = tmp_path
    service.wechat_detected = {service.session_id: ("one", "two")}
    assert call(service, "wechat.select", {"login": "one", "expected": 0})["ok"]
    sent, closed = [], []

    class Worker:
        def __init__(self, **kwargs):
            pass

        async def request(self, command, **args):
            sent.append((command, args))
            if len(sent) == 2:
                assert call(service, "wechat.select", {"login": "two", "expected": 1})["ok"]
            return {"items": [{"name": "合成好友", "username": "peer"}], "page": 1, "pages": 1, "total": 1}

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(receiver_transport, "SubprocessTransport", Worker)
    args = {"login": "one", "expected": 1, "kind": "private", "query": "合成", "page": 1}
    assert call(service, "wechat.directory_page", {**args, "page": 0})["error"] == "management_request"
    assert not sent
    assert call(service, "wechat.directory_page", args)["result"]["total"] == 1
    assert sent[0] == ("setup_directory_page", {"login": "one", "kind": "private", "query": "合成", "page": 1})
    assert call(service, "wechat.directory_page", args)["error"] == "edit_conflict"
    assert call(service, "wechat.directory_page", args)["error"] == "edit_conflict"
    assert len(sent) == len(closed) == 2
    assert ReplyAuthorizations(service.store).records() == []


def test_empty_manual_list_can_explicitly_enable_and_cancel_follow_all(service,tmp_path,monkeypatch):
    from companion_wechat import receiver_transport
    from qiban.desktop_wechat_targets import identity
    service.wechat_resources = tmp_path
    for name in ("Qiban.WeChatReader.exe","bridge.dll"):
        (tmp_path/name).touch()
    service.wechat_detected={service.session_id:("one","two")}
    assert call(service,"wechat.select",{"login":"one","expected":0})["ok"]
    class Worker:
        def __init__(self,**kwargs): pass
        async def request(self,command,**args):
            assert command=="setup_save_check" and args["targets"]==[]
            return {"directory":str(tmp_path),"verified":0,"unavailable":[]}
        async def close(self): pass
    monkeypatch.setattr(receiver_transport,"SubprocessTransport",Worker)
    args={"login":"one","expected":1}
    old=call(service,"wechat.targets.saved",args)["result"]
    assert call(service,"wechat.targets.save",{**args,"targets":[],"version":old["version"]})["ok"]
    runtime=DesktopRuntime(service,"audience-app",model=None)
    try:
        def ask(enabled):
            return call(service,"audience.preview",{"platform":"wechat","kind":"group",
                        "account":identity("one"),"enabled":enabled})["result"]
        prompt=ask(True)
        assert "one" in prompt["message"] and "积压" in prompt["message"] and "wechat" not in runtime.cores
        assert call(service,"audience.save",{"token":prompt["token"]})["result"]["enabled"]
        assert "wechat" in runtime.cores and not service.store.state("core-run:wechat",{}).get("generation")
        assert call(service,"wechat.targets.saved",args)["result"]["targets"]==[]
        off=ask(False)
        assert not call(service,"audience.save",{"token":off["token"]})["result"]["enabled"]
        assert "wechat" not in runtime.cores
        stale=ask(True)
        assert call(service,"wechat.select",{"login":"two","expected":1})["ok"]
        assert call(service,"wechat.select",{"login":"one","expected":2})["ok"]
        assert call(service,"audience.save",{"token":stale["token"]})["error"]=="edit_conflict"
    finally:
        runtime.close()


def test_manual_list_save_loss_return_and_explicit_removal(service,tmp_path,monkeypatch):
    from companion_core.reply_authorization import ReplyScope
    from companion_wechat import receiver_transport
    from companion_wechat.receiver import DatabaseGateway
    from qiban.desktop_wechat import _profile, enrollment_key
    from qiban.desktop_wechat_targets import identity

    service.wechat_resources = tmp_path
    for name in ("Qiban.WeChatReader.exe", "bridge.dll"):
        (tmp_path/name).touch()
    service.wechat_detected = {service.session_id: ("one",)}
    assert call(service,"wechat.select",{"login":"one","expected":0})["ok"]
    selected={"login":"one","expected":1}
    targets=[{"kind":"private","username":"friend"},{"kind":"group","username":"group@chatroom"}]
    missing=set()
    class Worker:
        def __init__(self,**kwargs): pass
        async def request(self,command,login,targets):
            gone=[t["username"] for t in targets if t["username"] in missing]
            result={"verified":len(targets)-len(gone),"unavailable":gone}
            if command=="setup_save_check":
                result["directory"]=str(tmp_path)
            return result
        async def close(self): pass
    monkeypatch.setattr(receiver_transport,"SubprocessTransport",Worker)
    def read(): return call(service,"wechat.targets.saved",selected)["result"]
    def save(items):
        return call(service,"wechat.targets.save",{**selected,"targets":items,"version":read()["version"]})
    def verify(): return call(service,"wechat.targets.verify",{**selected,"targets":targets})
    assert save(targets)["ok"]
    mod=adapter(service.store,service.installation,tmp_path)
    scope=ReplyScope("wechat",identity("one"),"private","friend")
    group=ReplyScope("wechat",identity("one"),"group","group@chatroom")
    permissions=ReplyAuthorizations(service.store)
    old,other=permissions.version(scope),permissions.version(group)
    assert old and other
    missing.add("friend")
    assert verify()["ok"] and permissions.version(scope) is None
    assert save(targets)["ok"] and len(read()["targets"])==2
    assert permissions.version(group)==other
    missing.add("unavailable")
    assert save(targets+[{"kind":"private","username":"unavailable"}])["error"]=="wechat_target_unavailable"
    assert len(read()["targets"])==2
    missing.clear()
    assert verify()["ok"]
    restored=permissions.record(scope)
    assert restored["enabled"] and restored["version"] != old
    assert permissions.version(scope,message_time=restored["messages_since"]-1) is None
    assert permissions.version(scope,message_time=restored["messages_since"])==restored["version"]
    with pytest.raises(AssistantError):
        permissions.check(scope,old)
    receiver=DatabaseGateway(service.store,{},account=mod.account)
    scoped,_=_profile(service.store, service.installation,"one")
    receiver._initialize(service.store.state(enrollment_key(scoped)))
    assert len(receiver._saved["enrollment"]["targets"])==2
    assert save([])["ok"] and not read()["targets"]
    assert adapter(service.store,service.installation,tmp_path) is None
    assert permissions.version(scope) is None


def test_switch_stops_old_generation_and_preserves_each_registered_source(service, tmp_path):
    from qiban.desktop_wechat import selection
    from tests_postgres.test_desktop_runtime import Adapter

    store, install = service.store, service.installation
    for name in ("Qiban.WeChatReader.exe", "bridge.dll"):
        (tmp_path / name).touch()
    def enroll(login):
        source = snapshot()
        source["enrollment"].update(self_username=login,
            account_id="wechat-db:" + hashlib.sha256(login.encode()).hexdigest(),
            account_directory_id=hashlib.sha256(tmp_path.name.encode()).hexdigest())
        with store.db() as db:
            register_source(store, install, [tmp_path], source, db=db)
        return adapter(store, install, tmp_path)

    first = enroll("wxid_first")
    permissions = ReplyAuthorizations(store)
    first_scope = first.reply_targets()[0].scope
    permissions.update(first_scope, enabled=True, expected_version=permissions.record(first_scope)["version"],
                       actor="test-admin")
    old = Adapter()
    old.platform, old.account = "wechat", first.account
    runtime = DesktopRuntime(service, "switch-test", model=object(), adapters=[old, Adapter()])
    service.wechat_resources = tmp_path
    try:
        core = runtime.cores["wechat"]
        generation = runtime.start("wechat")["run"]["generation"]
        other = runtime.start("qq")["run"]["generation"]
        def choose(login, expected):
            return call(service, "wechat.select", {"login": login, "expected": expected})
        assert choose("wxid_unknown", 1)["error"] == "wechat_account_unknown"
        core.guard(generation)
        service.wechat_detected = {service.session_id: ("wxid_second",)}
        assert choose("wxid_second", 1)["ok"]
        with pytest.raises(AssistantError):
            core.guard(generation)
        assert old.stopped == [generation]
        assert "wechat" not in runtime.cores
        runtime.cores["qq"].guard(other)
        second = enroll("wxid_second")
        assert second.account != first.account
        assert not permissions.record(second.reply_targets()[0].scope)["enabled"]
        assert choose("wxid_first", 1)["error"] == "edit_conflict"
        assert choose("wxid_first", 2)["ok"]
        assert adapter(store, install, tmp_path).account == first.account
        assert permissions.record(first_scope)["enabled"]
        assert selection(store, install)["accounts"] == ["wxid_first", "wxid_second"]
        runtime.cores["qq"].guard(other)
    finally:
        runtime.close()


def test_preexisting_registration_keeps_its_collector_and_history(service, tmp_path):
    from companion_wechat.collector import collector_account
    from qiban.desktop_wechat import SETUP, enrollment_key, selected_collector, selection

    store, install = service.store, service.installation
    state = snapshot()
    store.set_state(enrollment_key(install), state)
    store.set_state(SETUP, {"directory": str(tmp_path), "identity": state["enrollment"]["account_id"]})
    login = state["enrollment"]["self_username"]
    assert selection(store, install)["selected"] == login
    assert selected_collector(store, install) == collector_account(install)
    service.wechat_detected = {service.session_id: ("wxid_other",)}
    assert call(service, "wechat.select", {"login":"wxid_other","expected":0})["ok"]
    assert call(service, "wechat.select", {"login":login,"expected":1})["ok"]
    assert store.state(enrollment_key(install)) == state
    assert selected_collector(store, install) == collector_account(install)


async def test_switch_after_reply_generation_before_native_submission(service):
    from tests_postgres.test_wechat_module import compose
    from tests_postgres.test_wechat_role import enqueue
    from tests_wechat.test_receiver import row

    service.wechat_detected = {service.session_id: ("one", "two")}
    assert call(service, "wechat.select", {"login": "one", "expected": 0})["ok"]
    env = await compose(service.store)
    runtime = DesktopRuntime(service, "switch-at-submit", model=env.model)
    runtime.cores["wechat"] = env.core
    changed = []
    async def switch_at_last_check(request):
        changed.append(call(service, "wechat.select", {"login": "two", "expected": 1})["ok"])
    env.driver.verify_membership = switch_at_last_check
    try:
        env.core.start()
        enqueue(env, row(1))
        event, = await env.adapter.collect_once()
        result = await env.core.handle(event)
        assert changed == [True] and len(env.model.calls) == 1
        assert env.driver.sent == [] and result["action"]["status"] != "succeeded"
        assert service.store.state("core-run:wechat")["status"] == "paused"
    finally:
        await env.adapter.close()
        runtime.close()


def test_first_setup_detection_is_manual_and_requires_unlocked_management(service, monkeypatch):
    scans = []

    async def detect(resources):
        scans.append(resources)
        return {"accounts": ["synthetic"]}

    monkeypatch.setattr("qiban.desktop_wechat.detect_accounts", detect)
    assert call(service, "audience.status", {"platform": "wechat"})["ok"] and not scans
    assert call(service, "wechat.accounts")["result"] == {"accounts": ["synthetic"],
           "profiles": {"accounts": [], "revision": 0, "selected": None}}
    assert call(service, "lock.enable", {"password": "Synthetic-Password-123"})["ok"]
    assert call(service, "session.lock")["ok"]
    assert call(service, "wechat.accounts")["error"] == "desktop_locked"
    assert len(scans) == 1


@pytest.mark.parametrize("group", [False,True], ids=["readded-friend","rejoined-group"])
async def test_rejoined_contact_cannot_submit_the_reply_prepared_before_removal(service, group):
    from companion_wechat.membership import MembershipCheck
    from tests_postgres.test_wechat_module import compose
    from tests_postgres.test_wechat_role import enqueue
    from tests_wechat.test_receiver import GROUP, OWNER, STAMP, row

    env = await compose(service.store, family=group)
    username = GROUP if group else OWNER
    target = {"kind":"group" if group else "private", "username":username}
    def message(i):
        item = row(i, group=group)
        if group:
            item["trigger"] = "mention"
        return item
    def observe(present):
        check = MembershipCheck(service.store,env.receiver.bindings.identity,[target])
        check.permissions.clock = lambda: STAMP + 3.25
        check.finish({"verified":int(present),"unavailable":[] if present else [username]})
    async def rejoined(request):
        observe(False)
        observe(True)
    try:
        env.core.start()
        enqueue(env,message(1))
        first, = await env.adapter.collect_once()
        old_check = env.driver.verify_membership
        env.driver.verify_membership = rejoined
        result = await env.core.handle(first)
        assert result["action"]["status"] != "succeeded" and env.driver.sent == []
        env.driver.verify_membership = old_check
        enqueue(env,message(2),message(4))
        fresh, = await env.adapter.collect_once()
        assert len(env.session.merged.for_event(fresh)["members"]) == 1
        assert (await env.core.handle(fresh))["action"]["status"] == "succeeded" and len(env.driver.sent)==1
    finally:
        env.core.stop()
        await env.adapter.close()


def test_batch_verify_rechecks_selected_account_before_return_and_grants_nothing(service, tmp_path, monkeypatch):
    from companion_wechat import receiver_transport
    service.wechat_resources = tmp_path
    service.wechat_detected = {service.session_id: ("one", "two")}
    assert call(service, "wechat.select", {"login":"one","expected":0})["ok"]
    sent = []
    class Worker:
        def __init__(self, **kwargs):
            pass
        async def request(self, command, **args):
            sent.append(command)
            assert args["login"] == "one"
            if len(sent) == 2:
                assert call(service, "wechat.select", {"login":"two","expected":1})["ok"]
            return {"verified":1,"unavailable":[]}
        async def close(self):
            pass
    monkeypatch.setattr(receiver_transport, "SubprocessTransport", Worker)
    args = {"login":"one", "expected":1, "targets":[{"kind":"private","username":"synthetic-peer"}]}
    assert call(service, "wechat.targets.verify", args)["result"]["verified"] == 1
    assert call(service, "wechat.targets.verify", args)["error"] == "edit_conflict"
    assert call(service, "wechat.targets.verify", args)["error"] == "edit_conflict"
    assert sent == ["setup_verify", "setup_verify"]
    assert ReplyAuthorizations(service.store).records() == []


def test_packaged_wechat_resumes_only_verified_setup(service,tmp_path):
    store=service.store
    install=service.installation
    root=tmp_path/"已登记账号"
    root.mkdir()
    resources=Path(os.environ["QIBAN_TEST_WECHAT_RESOURCES"]) if "QIBAN_TEST_WECHAT_RESOURCES" in os.environ else tmp_path
    if resources==tmp_path:
        for name in ("Qiban.WeChatReader.exe","bridge.dll"):
            (resources/name).touch()
    assert adapter(store,install,resources) is None
    state=snapshot()
    state["enrollment"]["account_directory_id"]=hashlib.sha256(root.name.encode()).hexdigest()
    with pytest.raises(AssistantError),store.db() as db:
        register_source(store,install,[str(tmp_path)],state,db=db)
    assert adapter(store,install,resources) is None
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        register_source(store,install,[str(tmp_path),str(root)],state,db=db)
    wechat=adapter(store,install,resources)
    assert wechat.options["verified_config"]["account_dirs"]==[str(root)]
    assert all(not ReplyAuthorizations(store).record(t.scope)["enabled"] for t in wechat.reply_targets())
    assert wechat.options["reader_executable"]==resources/"Qiban.WeChatReader.exe"
    assert adapter(store,"other-installation",resources) is None
    runtime=DesktopRuntime(service,"synthetic-application",adapters=(wechat,))
    try:
        status=next(p for p in runtime.status()["platforms"] if p["platform"]=="wechat")
        assert status["available"] and not status["autorun"] and status["status"]!="active"
    finally:
        runtime.close()

@pytest.mark.parametrize('change', ['none', 'wrong_identity', 'locked', 'redetected'])
def test_account_details_require_current_detection_and_discard_late_metadata(service, monkeypatch, tmp_path, change):
    from companion_wechat import receiver_transport

    service.wechat_resources = tmp_path
    (tmp_path/'Qiban.WeChatReader.exe').touch()
    service.wechat_detected = {service.session_id: ('one',)}
    if change == 'locked':
        assert call(service, 'lock.enable', {'password': 'Synthetic-Password-123'})['ok']
        service.wechat_detected = {service.session_id: ('one',)}
    closed = []

    class Worker:
        def __init__(self, **kwargs):
            pass

        async def request(self, command, **kwargs):
            assert command == 'account_details' and kwargs == {'login': 'one'}
            if change == 'locked':
                assert call(service, 'session.lock')['ok']
            if change == 'redetected':
                service.wechat_detected = {}
            return {'login': 'other' if change == 'wrong_identity' else 'one',
                    'nickname': '合成昵称', 'wechat_id': 'synthetic-id'}

        async def close(self):
            closed.append(True)
    monkeypatch.setattr(receiver_transport, 'SubprocessTransport', Worker)
    result = call(service, 'wechat.account_details', {'login': 'one'})
    assert result['ok'] == (change == 'none')
    if result['ok']:
        assert result['result'] == {'login': 'one', 'nickname': '合成昵称', 'wechat_id': 'synthetic-id'}
    else:
        assert 'synthetic-id' not in str(result)
    assert closed == [True]


def test_account_details_cannot_read_an_undetected_account(service):
    service.wechat_detected = {service.session_id: ('one',)}
    assert call(service, 'wechat.account_details', {'login': 'other'})['error'] == 'wechat_account_unknown'
