"""Account-scoped manual selections, committed together with reader positions."""
import hashlib
import math
import secrets
from pathlib import Path

from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_wechat.collector import collector_account
from companion_wechat.durable_inbox import DurableInbox
from companion_wechat.receiver import _snapshot

from .desktop_wechat import _contact_request, _profile, adapter, enrollment_key, selection


def identity(login):
    return "wechat-db:" + hashlib.sha256(login.encode()).hexdigest()


def saved(manager, login, expected, db=None):
    store = manager.store
    active = selection(store, manager.installation, db)
    if not login or active["selected"] != login or type(expected) is not int or expected != active["revision"]:
        raise AssistantError("edit_conflict", "当前微信账号已变化，请重新检测")
    scoped, key = _profile(store, manager.installation, login, db)
    source = store.state(key, {}, db)
    pointer = store.state(enrollment_key(scoped), {}, db)
    grants = ReplyAuthorizations(store)
    targets = [t for t in pointer.get("enrollment", {}).get("targets", {}).values()
               if grants.record(ReplyScope("wechat", identity(login), t["kind"], t["username"]), db=db)["enabled"]]
    return {"targets": targets, "version": stable_key("wechat-list", source.get("revision", 0), targets)}


def _reshape(previous, login, directory, targets, since, bindings, retained):
    old = previous.get("enrollment", {})
    old_targets = old.get("targets", {})
    aliases = {t["username"]: alias for alias, t in old_targets.items()}
    aliases.update({b["target_ref"]: alias for alias, b in bindings.items()})
    selected = {aliases.get(t["username"], "target_" + hashlib.sha256(t["username"].encode()).hexdigest()[:32]): t
                for t in targets}
    times = {a: old.get("target_since", {}).get(a, old.get("since", since))
             if a in old_targets and t["username"] in retained else since for a,t in selected.items()}
    enrollment = {"schema_version": 2, "epoch": old.get("epoch") or secrets.token_hex(16),
                  "account_id": identity(login), "self_username": login, "since": old.get("since", since),
                  "account_directory_id": hashlib.sha256(directory.name.encode()).hexdigest(),
                  "targets": selected, "target_since": times}
    if old and any(enrollment[k] != old[k] for k in ("account_id","account_directory_id","self_username")):
        raise AssistantError("wechat_account_mismatch", "当前微信账号资料与原登记不一致")
    ck = previous.get("checkpoint", {})
    sources = {k: i for k, i in ck.get("sources", {}).items() if i["target_ref"] in selected}
    checkpoint = {"schema_version": 1, "epoch": enrollment["epoch"], "sources": sources}
    if ck.get("next_source") in sources:
        checkpoint["next_source"] = ck["next_source"]
    result = {"enrollment": enrollment, "checkpoint": checkpoint}
    if "window" in previous:
        result["window"] = {"epoch": enrollment["epoch"], "sources": {
            k: v for k, v in previous["window"]["sources"].items() if k in sources}}
    if "history" in previous:
        result["history"] = {k: v for k, v in previous["history"].items() if k in selected}
    return _snapshot(result)


async def save(manager, request, login, expected, targets, version):
    store = manager.store
    before = saved(manager, login, expected)
    if before["version"] != version:
        raise AssistantError("edit_conflict", "回复名单已变化，请重新读取")
    old_ids = {t["username"] for t in before["targets"]}
    result = await _contact_request(manager, request, login, expected, "setup_save_check", targets=targets)
    if set(result["unavailable"]) - old_ids:
        raise AssistantError("wechat_target_unavailable", "有新勾选的会话已不在好友或群列表中，请重新核验")
    directory = Path(result["directory"])
    if not directory.is_absolute():
        raise AssistantError("wechat_account_mismatch", "无法核实微信账号目录")
    scoped, key = _profile(store, manager.installation, login)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        manager.authority.require(request, administrator=True)
        if saved(manager, login, expected, db)["version"] != version:
            raise AssistantError("edit_conflict", "回复名单已变化，请重新读取")
        if store.state("core-run:wechat", {}, db).get("status") == "active":
            raise AssistantError("platform_active", "请停止微信回复后再保存名单")
        account = collector_account(scoped)
        inbox = DurableInbox(store, identity(login))
        cursor_key = "database-source:" + stable_key("logical-account", account)
        cursor = inbox.checkpoint(cursor_key)
        pointer = store.state(enrollment_key(scoped), {}, db)
        bindings = store.state(stable_key("wechat-bindings-v1", account), {}, db).get("conversations", {})
        updated = _reshape(cursor or pointer, login, directory, targets, math.ceil(manager.clock()), bindings, old_ids)
        inbox.publish_batch([], checkpoint_key=cursor_key, expected_checkpoint=cursor, checkpoint=updated, db=db)
        store.set_state(enrollment_key(scoped), {k: updated[k] for k in ("enrollment","checkpoint")}, db)
        previous = store.state(key, {}, db)
        store.set_state(key, {"directory": str(directory), "identity": identity(login),
                              "revision": previous.get("revision", 0) + 1}, db)
        desired = {t["username"] for t in targets}
        grants = ReplyAuthorizations(store)
        for t in [*targets, *(t for t in before["targets"] if t["username"] not in desired)]:
            scope = ReplyScope("wechat", identity(login), t["kind"], t["username"])
            record = grants.record(scope, db=db)
            grants.update(scope, enabled=t["username"] in desired, expected_version=record["version"],
                          actor=request.actor, db=db)
        response = saved(manager, login, expected, db)
    if manager.runtime is not None:
        manager.runtime.configure_platform("wechat", adapter(store, manager.installation, manager.wechat_resources))
    return response
