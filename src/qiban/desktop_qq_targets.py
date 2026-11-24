"""Verified QQ reply scopes. Discovery is separate from explicit permission."""
import secrets
from copy import deepcopy

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_qq.config import native_parameters, qq_id, validate_native
from companion_qq.module import QQModule, digest, enrollment_intent
from companion_qq.native_provider import NativeProvider


def directory(worker, options):
    identity = worker.open()
    if identity.get("uin") != options["account"] or identity.get("uid") != options["uid"]:
        raise AssistantError("qq_account_mismatch", "QQ 登录账号与所选账号不一致。")
    value = worker.request("directory")
    if not isinstance(value, dict) or set(value) != {"private_targets", "groups"}:
        raise AssistantError("qq_native_catalog_invalid", "QQ 名称列表格式无效。")
    for rows in value.values():
        if not isinstance(rows, list) or len(rows) > 10000:
            raise AssistantError("qq_native_catalog_invalid", "QQ 名称列表超过限制。")
        for row in rows:
            if (not isinstance(row, dict) or not {'id', 'label'} <= set(row)
                    or set(row) - {'id', 'label', 'nickname'}
                    or any(not isinstance(row.get(key, ''), str) or len(row.get(key, '')) > 128
                           for key in ('label', 'nickname'))):
                raise AssistantError("qq_native_catalog_invalid", "QQ 名称列表格式无效。")
            qq_id(row["id"])
    current = worker.request("identity")
    if current.get("uin") != options["account"] or current.get("uid") != options["uid"]:
        raise AssistantError("qq_account_mismatch", "QQ 登录账号已改变。")
    return deepcopy(value)


def configuration(manager, saved, private_targets, groups):
    selected = saved["selected"]
    if selected is None:
        raise AssistantError("qq_login_required", "请先保存 QQ 账号选择。")
    return validate_native({"version": 2, "transport": "native", "account": selected["uin"],
        "uid": selected["uid"], "runtime": str(manager.runtime), "data": str(manager.data),
        "private_targets": private_targets, "groups": groups})


def scopes(options):
    return [ReplyScope("qq", options["account"], kind, target)
            for kind, field in (("private", "private_targets"), ("group", "groups"))
            for target in options[field]]


def verify(manager, worker, options, revision):
    from companion_core.group_modes import settings
    manual_groups = list(options["groups"])
    saved = manager.store.state("desktop-qq-account", {})
    existing = saved.get("configurations", {}).get(options["account"], {}).get("groups", [])
    options = {**options, "groups": list(dict.fromkeys(manual_groups + [target for target in existing
        if settings(manager.store, ReplyScope("qq", options["account"], "group", target))["listen"]]))}
    provider = NativeProvider(manager.store, native_parameters(options), worker_factory=lambda _: worker)
    identity = provider.probe()
    authorizations = ReplyAuthorizations(manager.store)
    versions = {scope.id: authorizations.record(scope)["version"] for scope in scopes(options)}
    return {"token": secrets.token_hex(16), "revision": revision, "options": deepcopy(options),
            "identity": identity, "versions": versions, "manual_groups": manual_groups}


def save(manager, preview, saved, actor, db):
    options = preview["options"]
    selected = saved["selected"]
    if preview["revision"] != saved["revision"] or selected is None or (
            selected["uin"], selected["uid"]) != (options["account"], options["uid"]):
        raise AssistantError("edit_conflict", "QQ 账号或会话设置已改变，请重新核验。")
    authorizations = ReplyAuthorizations(manager.store)
    desired = {scope.id: scope for scope in scopes(options)}
    if "manual_groups" in preview:
        desired = {k: s for k, s in desired.items() if s.kind != "group" or s.target_id in preview["manual_groups"]}
    # Revoke removed targets on this account, including previously retained scopes.
    for record in authorizations.records():
        scope = ReplyScope(**record["scope"])
        if scope.platform == "qq" and scope.account_identity == selected["uin"] and scope.id not in desired:
            if record["enabled"]:
                authorizations.update(scope, enabled=False, expected_version=record["version"], actor=actor, db=db)
    for identifier, scope in desired.items():
        authorizations.update(scope, enabled=True, expected_version=preview["versions"][identifier], actor=actor, db=db)
    configurations = deepcopy(saved.get("configurations", {}))
    configurations[selected["uin"]] = {"uid": selected["uid"],
        "private_targets": options["private_targets"], "groups": options["groups"],
        "manual_groups": preview.get("manual_groups", options["groups"])}
    # Enrollment and authorization commit together. Neither starts a role.
    manager.store.set_state("qq-enrollment:" + selected["uin"],
                            {"intent": enrollment_intent(options), "identity": preview["identity"]}, db)
    return {**saved, "revision": saved["revision"] + 1, "configurations": configurations}


def saved_options(manager, saved):
    selected = saved["selected"]
    value = saved.get("configurations", {}).get(selected["uin"]) if selected else None
    if value is None or value["uid"] != selected["uid"]:
        return None
    return configuration(manager, saved, value["private_targets"], value["groups"])


def _observe_group(manager, account, target, db):
    """Directory evidence registers a receive target. Reply grants stay with save()."""
    store = manager.store
    saved = store.state("desktop-qq-account", {}, db)
    configs = deepcopy(saved.get("configurations", {}))
    current = configs.get(account)
    if current and target in current["groups"]:
        return
    selected = saved.get("selected") or {}
    if selected.get("uin") != account or store.state("core-run:qq", {}, db).get("status") == "active":
        raise AssistantError("platform_active", "请在所选账号停止运行后登记新旁听群")
    config = current or {"uid": selected["uid"], "private_targets": [], "groups": []}
    config.setdefault("manual_groups", list(config["groups"]))
    config["groups"] = [*config["groups"], target]
    options = {"version": 2, "transport": "native", "account": account,
               **{key: config[key] for key in ("uid", "private_targets", "groups")}}
    previous = store.state("qq-enrollment:" + account, {}, db).get("identity", {})
    configs[account] = config
    store.set_state("qq-enrollment:" + account, {"intent": enrollment_intent(options), "identity": {
        **previous, "account": account, "account_label": selected.get("label") or account,
        "private_targets": config["private_targets"], "groups": config["groups"]}}, db)
    store.set_state("desktop-qq-account", {**saved, "configurations": configs, "revision": saved["revision"]+1}, db)


def adapter(manager):
    from .desktop_qq import KEY

    saved = manager.store.state(KEY, {"revision": 0, "selected": None})
    options = saved_options(manager, saved)
    if options is None:
        return None
    # Upgrade only a fully matching old enrollment; never infer consent from a
    # changed account/UID/target list or from a missing registration record.
    with manager.store.lock, manager.store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        key = "qq-enrollment:" + options["account"]
        enrollment = manager.store.state(key, None, db)
        if enrollment and enrollment.get("intent") == digest(options):
            manager.store.set_state(key, {**enrollment, "intent": enrollment_intent(options)}, db)
    return QQModule(manager.store, manager.data.parent, options=options)
