"""Packaged WeChat resources and installation-owned, verified first enrollment."""
from pathlib import Path

from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_wechat.collector import collector_account
from companion_wechat.db_source import validate_checkpoint, validate_enrollment
from companion_wechat.module import WeChatModule
from companion_wechat.windows_version import unsupported_message, version_status

SETUP = "desktop-wechat-source-v1"


async def detect_accounts(resources):
    from companion_wechat.native.protocol import personal
    from companion_wechat.receiver_transport import SubprocessTransport, WorkerError

    reader = Path(resources) / "Qiban.WeChatReader.exe" if resources else None
    if not reader or not reader.is_file():
        raise AssistantError("wechat_reader_missing", "本次安装缺少微信读取组件")
    worker = SubprocessTransport(executable=reader, read_timeout=10)
    try:
        result = await worker.request("logged_accounts")
        accounts = result.get("accounts") if isinstance(result, dict) else None
        if not isinstance(accounts, list) or not all(personal(name) for name in accounts):
            raise AssistantError("wechat_account_detection", "无法核实微信登录账号，请重新检测")
        if not accounts:
            status = version_status()
            if status["running_versions"] and status["running_versions"] == status["unrecognized_versions"]:
                raise AssistantError("client_version_unsupported", unsupported_message(status))
        return {"accounts": accounts}
    except WorkerError as exc:
        if exc.code == "client_version_unsupported":
            raise AssistantError(exc.code, unsupported_message()) from None
        raise AssistantError(exc.code, "无法核验当前登录账号，请确认微信已登录后重新检测") from None
    finally:
        await worker.close()


def enrollment_key(installation):
    return "wechat-db-enrollment:" + stable_key("logical-account",collector_account(installation))


async def account_details(manager, request, login):
    from companion_wechat.native.protocol import personal
    from companion_wechat.receiver_transport import SubprocessTransport

    def check():
        manager.authority.require(request, administrator=True)
        if not personal(login) or login not in manager.wechat_detected.get(request.session_id, ()):
            raise AssistantError("wechat_account_unknown", "请先检测已登录的微信账号")
    check()
    reader = manager.wechat_resources / "Qiban.WeChatReader.exe" if manager.wechat_resources else None
    if not reader or not reader.is_file():
        raise AssistantError("wechat_reader_missing", "本次安装缺少微信读取组件")
    worker = SubprocessTransport(executable=reader, read_timeout=45)
    try:
        result = await worker.request("account_details", login=login)
        check()
        if (not isinstance(result, dict) or set(result) != {"login", "nickname", "wechat_id"}
                or result["login"] != login or any(not isinstance(result[k], str) or len(result[k]) > 200
                    or any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 for c in result[k]) for k in ("nickname", "wechat_id"))):
            raise AssistantError("wechat_account_detection", "无法核实微信账号资料，请重新检测")
        return result
    finally:
        await worker.close()


async def _contact_request(manager, request, login, expected, command, **args):
    from companion_wechat.receiver_transport import SubprocessTransport

    def check():
        manager.authority.require(request, administrator=True)
        active = selection(manager.store, manager.installation)
        if (active["selected"] != login or not login or type(expected) is not int
                or active["revision"] != expected):
            raise AssistantError("edit_conflict", "微信账号已变化，请重新检测")
    check()
    observation = None
    if command in ("setup_verify", "setup_save_check"):
        from companion_wechat.membership import MembershipCheck

        from .desktop_wechat_targets import identity
        targets = args.get("targets")
        if (not isinstance(targets, list) or any(not isinstance(t,dict) or set(t) != {"kind","username"}
                or not isinstance(t["username"],str) or t["kind"] not in ("private","group") for t in targets)
                or len({t["username"] for t in targets}) != len(targets)):
            raise AssistantError("management_request", "微信会话列表格式无效")
        observation = MembershipCheck(manager.store, identity(login), targets)
    worker = SubprocessTransport(executable=manager.wechat_resources / "Qiban.WeChatReader.exe", read_timeout=45)
    try:
        result = await worker.request(command, login=login, **args)
        check()
        if observation is not None:
            observation.finish(result)
        return result
    finally:
        await worker.close()


async def directory_page(manager, request, login, expected, kind, query, page):
    if (kind not in ("private", "group") or not isinstance(query, str) or len(query) > 100
            or type(page) is not int or not 1 <= page <= 10000):
        raise AssistantError("management_request", "微信目录分页无效")
    return await _contact_request(manager, request, login, expected, "setup_directory_page",
                                  kind=kind, query=query, page=page)


async def directory_sync(manager, request, login, expected):
    return await _contact_request(manager, request, login, expected, 'setup_directory_sync')


async def directory(manager, request, login, expected, kind, after):
    if kind not in ("private", "group") or not isinstance(after, str) or len(after) > 256:
        raise AssistantError("management_request", "微信目录分页无效")
    return await _contact_request(manager, request, login, expected, "setup_directory", kind=kind, after=after)


async def verify_targets(manager, request, login, expected, targets):
    return await _contact_request(manager, request, login, expected, "setup_verify", targets=targets)


def _legacy(store, installation, db=None):
    setup = store.state(SETUP, db=db)
    snapshot = store.state(enrollment_key(installation), db=db) if setup else None
    if snapshot and snapshot["enrollment"]["account_id"] == setup["identity"]:
        return snapshot["enrollment"]["self_username"]


def selection(store, installation, db=None):
    key = stable_key("desktop-wechat-selection", installation)
    value = store.state(key, db=db)
    if value is not None:
        return value
    old = _legacy(store, installation, db)
    return {"revision": 0, "selected": old, "accounts": [old] if old else []}


def save_selection(store, installation, login, old, db):
    value = {"revision": old["revision"] + 1, "selected": login,
             "accounts": sorted(set(old["accounts"]) | {login})}
    store.set_state(stable_key("desktop-wechat-selection", installation), value, db)
    return value


def _profile(store, installation, login, db=None):
    if login == _legacy(store, installation, db):
        return installation, SETUP
    scoped = stable_key("desktop-wechat-profile", installation, login)
    return scoped, scoped


def selected_collector(store, installation):
    login = selection(store, installation)["selected"]
    if login:
        scoped, _ = _profile(store, installation, login)
        return collector_account(scoped)


def register_source(store, installation, directories, snapshot, *, db):
    """Caller supplies the authenticated reader result and owns the enrollment transaction."""
    enrollment=validate_enrollment(snapshot["enrollment"])
    checkpoint=validate_checkpoint(snapshot["checkpoint"],enrollment)
    login = enrollment["self_username"]
    old = selection(store, installation, db)
    if old["selected"] and old["selected"] != login:
        raise AssistantError("edit_conflict", "所选微信账号已变化，请重新登记")
    scoped, setup_key = _profile(store, installation, login, db)
    key=enrollment_key(scoped)
    if store.state(key, db=db) is not None or store.state(setup_key, db=db) is not None:
        raise AssistantError("wechat_already_enrolled","微信已登记，请先核对现有账号。")
    # Keep only the directory that actually authenticated; other candidates are not needed on reconnect.
    import hashlib
    matches=[p for p in map(Path,directories) if p.is_absolute() and
             hashlib.sha256(p.name.encode()).hexdigest()==enrollment["account_directory_id"]]
    if len(matches)!=1:
        raise AssistantError("wechat_account_mismatch","当前微信账号目录与核验结果不一致。")
    store.set_state(key,{"enrollment":enrollment,"checkpoint":checkpoint},db)
    store.set_state(setup_key,{"directory":str(matches[0]),"identity":enrollment["account_id"]},db)
    if not old["selected"]:
        save_selection(store, installation, login, old, db)


def adapter(store, installation, resources):
    if resources is None:
        return None
    root=Path(resources)
    reader, bridge = root/"Qiban.WeChatReader.exe", root/"bridge.dll"
    login = selection(store, installation)["selected"]
    if not login:
        return None
    scoped, setup_key = _profile(store, installation, login)
    source=store.state(setup_key)
    if not source or not reader.is_file() or not bridge.is_file():
        return None
    snapshot=store.state(enrollment_key(scoped))
    if not snapshot:
        return None
    enrollment=validate_enrollment(snapshot["enrollment"])
    if enrollment["account_id"]!=source["identity"] or enrollment["self_username"] != login:
        return None
    if not enrollment["targets"]:
        from companion_core.reply_audience import Audience, read
        if not any(read(store,Audience("wechat",source["identity"],kind))["enabled"] for kind in ("private","group")):
            return None
    config={"account_dirs":[source["directory"]],"target_hints":[
        {"kind":row["kind"],"hint":row["username"]} for row in enrollment["targets"].values()]}
    module = WeChatModule(store,root,scoped,options={
        "reader_executable":reader,"verified_config":config,"native_dll":bridge})
    module.setup_revision = source.get("revision", 0)
    return module


def select_account(manager, request, login, expected):
    from companion_wechat.native.protocol import personal

    if not personal(login) or type(expected) is not int:
        raise AssistantError("management_request", "请选择已核验的微信账号")
    store, install, runtime = manager.store, manager.installation, manager.runtime
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        manager.authority.require(request, administrator=True)
        old = selection(store, install, db)
        if old["revision"] != expected:
            raise AssistantError("edit_conflict", "微信账号设置已变化，请重新读取")
        if old["selected"] == login:
            return old
        if login not in old["accounts"] and login not in manager.wechat_detected.get(request.session_id, ()):
            raise AssistantError("wechat_account_unknown", "请先检测已登录的微信账号")
        if runtime:
            runtime.control(request, "wechat", "stop")
        value = save_selection(store, install, login, old, db)
        manager.repository.audit(request.actor, "desktop_wechat_select", install, db=db)
    if runtime:
        runtime.configure_platform("wechat", adapter(store, install, manager.wechat_resources))
    return value
