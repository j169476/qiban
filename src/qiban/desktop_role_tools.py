"""Administrator bridge for per-group observation and scoped reminder maintenance."""
from companion_core import group_modes, reminders
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_exclusion import ReplyExclusions

from .desktop_reply_quota import accounts


def catalog_key(platform, account):
    return stable_key("verified-group-directory", platform, account)


def remember_directory(store, platform, account, groups, *, complete=False):
    key = catalog_key(platform, account)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        names = {} if complete else store.state(key, {}, db)
        names.update(groups)
        store.set_state(key, names, db)


def row(store, scope, name, db, *, enrolled):
    mode = group_modes.settings(store, scope, db)
    qq = scope.platform == "qq"
    capabilities = {"listen": qq or enrolled, "proactive": qq, "reminder_delivery": qq,
                    "reason": "" if qq else ("个人微信当前仅支持已登记来源的旁听和原回复。" if enrolled
                                              else "此群尚无受支持的收件来源。")}
    return {"target": scope.target_id, "name": name or scope.target_id,
            "capabilities": capabilities,
            "reply_authorized": ReplyAuthorizations(store).version(scope, db=db) is not None,
            "excluded": ReplyExclusions(store).record(scope, db=db)["excluded"],
            **{k: mode[k] for k in ("listen", "proactive", "version")}}


def registered(manager, platform, account, db):
    from .desktop_wechat import selection
    from .desktop_wechat_targets import identity

    if platform not in ("qq", "wechat") or not isinstance(account, str):
        raise AssistantError("management_request", "平台或账号无效")
    entry = next((e for e in accounts(manager, db) if (e["platform"], e["account"]) == (platform, account)), None)
    names = dict(manager.store.state(catalog_key(platform, account), {}, db))
    if entry:
        names.update({t: entry["names"].get(t) or names.get(t, t) for t, kind in entry["targets"].items() if kind == "group"})
        return entry["account_identity"], names, entry["targets"]
    selected = (manager.store.state("desktop-qq-account", {}, db).get("selected") or {}).get("uin") if platform == "qq" else (
        selection(manager.store, manager.installation, db)["selected"])
    if not account or selected != account:
        raise AssistantError("edit_conflict", "账号已经改变，请重新读取")
    return (account if platform == "qq" else identity(account)), names, {}


def operate(manager, request, op, args):
    store = manager.store
    manager.authority.require(request, administrator=True)
    if op == "reminders.list":
        from .desktop_matters import reminder_row
        page = reminders.listing(store, **args, project=lambda value: reminder_row(store, value))
        return {**page, "capabilities": {
            "qq": {"private": True, "group": True}, "bilibili": {"private": True, "group": False},
            "wechat": {"private": False, "group": False}}}
    if op in {"reminders.cancel", "reminders.reschedule"}:
        if not isinstance(args["id"], str) or not isinstance(args["expected"], str):
            raise AssistantError("management_request", "提醒标识或版本无效")
        from .desktop_matters import reminder_row
        return reminders.change(store, args["id"], args["expected"], op.split(".")[1], manager.clock(),
                                due_at=args.get("due_at"), project=lambda value: reminder_row(store, value))
    platform, account = args["platform"], args["account"]
    with store.db() as db:
        identity, names, enrolled = registered(manager, platform, account, db)
        def item(target):
            return row(store, ReplyScope(platform, identity, "group", target), names[target], db,
                       enrolled=enrolled.get(target) == "group")
        if op == "group_modes.list":
            after, target = args["after"], args.get("target")
            if after is not None and not isinstance(after, str) or target is not None and not isinstance(target, str):
                raise AssistantError("management_request", "群分页参数无效")
            keys = ([target] if target in names else []) if target is not None else sorted(k for k in names if not after or k > after)
            return {"items": [item(k) for k in keys[:30]], "next": keys[29] if len(keys) > 30 else None}
        target = args["target"]
        if not isinstance(target, str) or target not in names:
            raise AssistantError("group_unknown", "请先在所选账号的目录核验此群")
        scope = ReplyScope(platform, identity, "group", target)
        capabilities = item(target)["capabilities"]
        if args["listen"] and (not capabilities["listen"] or args["proactive"] and not capabilities["proactive"]):
            raise AssistantError("group_source_unavailable", capabilities["reason"])
        before = group_modes.settings(store, scope, db)
        value = group_modes.save(store, scope, args["expected"], args["listen"], args["proactive"], manager.clock(), db)
        if platform == "qq" and value["listen"] and not before["listen"]:
            from .desktop_qq_targets import _observe_group
            _observe_group(manager, account, target, db)
        return item(target)
