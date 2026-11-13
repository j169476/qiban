"""Reply quota editing for accounts that are saved and were verified before.

A reply quota is a reply count and a group participation limit, never a paid
token, and saving it never calls a model. This module changes only
``companion_core.reply_policy`` records: it does not authorize a conversation,
does not start or stop a platform, and never contacts a connector, so an offline
installation keeps the saved defaults editable and keeps every already counted
usage.

Registration evidence is read from the durable records the desktop app already
writes (``desktop-qq-account`` plus ``qq-enrollment:<uin>``, and the WeChat
selection with its installation-owned profile and enrollment snapshot). A page
supplied account or scope is never trusted on its own. Authorization display reuses
``reply_audience.read`` and ``ReplyAuthorizations.version``: a manual pick, an
allow-all policy and the currently effective eligibility are three different things.
A single-conversation exclusion (``companion_core.reply_exclusion``) is shown here
because it wins over the other two states, but only ``desktop_reply_exclusion``
writes it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience
from companion_core.reply_audience import read as audience_read
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_exclusion import ReplyExclusions
from companion_core.reply_policy import BOUNDS, DEFAULTS, Participation, ReplyPolicies

PAGE = 20
ACCOUNT_PAGE = 20
QQ_KEY = "desktop-qq-account"
QQ_ENROLLMENT = "qq-enrollment:"
CORE_RUN = "core-run:"
IDENTITY = re.compile(r"[A-Za-z0-9_.:@-]{1,256}")
FIELDS = {"private_daily_replies": "私聊每自然日回复数",
          "group_session_replies": "群参与每阶段回复数",
          "group_session_decisions": "群参与每阶段决策数",
          "group_session_minutes": "群参与阶段时长（分钟）"}
PRIVATE_FIELDS = ("private_daily_replies",)
GROUP_FIELDS = ("group_session_replies", "group_session_decisions", "group_session_minutes")
REASONS = {"participation_complete": "阶段内的回复或决策已用完",
           "reply_quota": "阶段回复数已达上限",
           "send_uncertain": "有一次结果不明的发送，阶段已结束",
           "topic_ended": "角色已结束本次参与",
           "quota_changed": "额度调整后按新上限结束"}


def _stop(code, message):
    raise AssistantError(code, message)


def _identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        _stop("reply_quota_invalid", "账号或会话标识格式无效，请重新读取。")
    return value


def _platform(value):
    if not isinstance(value, str) or value not in ("qq", "wechat"):
        _stop("reply_quota_invalid", "暂不支持该平台的回复额度。")
    return value


def _cursor(value):
    if not isinstance(value, str) or len(value) > 256 or (value and not IDENTITY.fullmatch(value)):
        _stop("reply_quota_invalid", "分页位置无效，请重新读取回复额度。")
    return value


def _limits(changes, allowed):
    """Same integer bounds as the core policy; only the message is friendlier."""
    if (not isinstance(changes, dict) or not changes or set(changes) - set(allowed)
            or any(type(value) is not int for value in changes.values())):
        _stop("reply_quota_invalid", "额度必须是整数，且只能修改私聊每日回复数与群参与上限。")
    for key, value in changes.items():
        low, high = BOUNDS[key]
        if not low <= value <= high:
            _stop("reply_quota_invalid", f"{FIELDS[key]}必须在 {low} 至 {high} 之间。")
    return changes


def _participation(manager):
    clock = getattr(manager, "clock", None)
    if clock is None:
        return Participation(manager.store)
    return Participation(manager.store, clock=lambda: datetime.fromtimestamp(clock(), timezone.utc))


def _day(store, participation):
    zone = store.read("policy")["data"]["timezone"]
    return participation.clock().astimezone(ZoneInfo(zone)).date().isoformat(), zone


def _qq_configuration(uin, value):
    """Re-validate the saved configuration exactly as the connector did."""
    from companion_qq.config import qq_id

    try:
        account = qq_id(uin)
    except AssistantError:
        return None
    if not isinstance(value, dict) or set(value) != {"uid", "private_targets", "groups"}:
        return None
    if not isinstance(value["uid"], str) or not re.fullmatch(r"u_.{1,126}", value["uid"]):
        return None
    result = {"uid": value["uid"]}
    for field in ("private_targets", "groups"):
        rows = value[field]
        if not isinstance(rows, list) or len(rows) > 10000:
            return None
        try:
            targets = [qq_id(row) for row in rows]
        except AssistantError:
            return None
        if len(set(targets)) != len(targets):
            return None
        result[field] = targets
    if account in result["private_targets"]:
        return None
    return result


def _qq_native(uin, config):
    return {"version": 2, "transport": "native", "account": uin, "uid": config["uid"],
            "private_targets": config["private_targets"], "groups": config["groups"]}


def _qq_enrolled(enrollment, uin, config, selected, qq):
    from companion_qq.module import digest, enrollment_intent

    if not isinstance(enrollment, dict) or set(enrollment) != {"intent", "identity"}:
        return False
    identity = enrollment["identity"]
    if not isinstance(identity, dict) or identity.get("account") != uin:
        return False
    # The probe recorded the exact target set that was verified; a later edit of
    # the saved configuration is a different registration, not a quota question.
    if (identity.get("private_targets") != config["private_targets"]
            or identity.get("groups") != config["groups"]):
        return False
    accepted = {enrollment_intent(_qq_native(uin, config))}
    if selected and selected.get("uin") == uin and qq is not None:
        # Older builds hashed the full native options including local paths, which
        # only the currently selected account can reproduce exactly.
        try:
            accepted.add(digest({**_qq_native(uin, config), "runtime": str(qq.runtime), "data": str(qq.data)}))
        except (AttributeError, TypeError, ValueError):
            pass
    return enrollment.get("intent") in accepted


def _qq_accounts(store, db=None, *, qq=None):
    saved = store.state(QQ_KEY, {"revision": 0, "selected": None}, db)
    if not isinstance(saved, dict):
        return []
    selected = saved.get("selected") if isinstance(saved.get("selected"), dict) else None
    configurations = saved.get("configurations")
    if not isinstance(configurations, dict):
        return []
    result = []
    for uin, value in configurations.items():
        config = _qq_configuration(uin, value)
        if config is None:
            continue
        account = str(uin)
        if not _qq_enrolled(store.state(QQ_ENROLLMENT + account, None, db), account, config, selected, qq):
            continue
        current = bool(selected and selected.get("uin") == account)
        label = selected.get("label") if current else None
        result.append({"platform": "qq", "account": account, "account_identity": account,
                       "label": label if isinstance(label, str) else None, "selected": current,
                       "names": {},
                       "targets": {**{row: "private" for row in config["private_targets"]},
                                   **{row: "group" for row in config["groups"]}}})
    return result


def _wechat_accounts(store, installation, db=None):
    from companion_wechat.db_source import SourceError, validate_enrollment
    from companion_wechat.native.protocol import personal

    from .desktop_wechat import _profile, enrollment_key, selection
    from .desktop_wechat_targets import identity as wechat_identity

    saved = selection(store, installation, db)
    logins = saved.get("accounts")
    candidates = [*(logins if isinstance(logins, list) else []), saved.get("selected")]
    result = []
    for login in dict.fromkeys(value for value in candidates if personal(value)):
        try:
            scoped, setup_key = _profile(store, installation, login, db)
            source = store.state(setup_key, None, db)
            if not isinstance(source, dict) or source.get("identity") != wechat_identity(login):
                continue
            snapshot = store.state(enrollment_key(scoped), None, db)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("enrollment"), dict):
                continue
            enrollment = validate_enrollment(snapshot["enrollment"])
        except (SourceError, AssistantError, KeyError, TypeError, ValueError):
            # A damaged or partial profile is not evidence; it never becomes trust.
            continue
        if enrollment["self_username"] != login:
            continue
        names = enrollment.get("display_names")
        result.append({"platform": "wechat", "account": login, "account_identity": wechat_identity(login),
                       "label": login, "selected": saved.get("selected") == login,
                       "names": {key: value for key, value in (names if isinstance(names, dict) else {}).items()
                                 if isinstance(value, str)},
                       "targets": {row["username"]: row["kind"] for row in enrollment["targets"].values()}})
    return result


def accounts(manager, db=None):
    """Every saved account whose registration can still be proven without a connector."""
    store = manager.store
    rows = [*_qq_accounts(store, db, qq=getattr(manager, "qq", None)),
            *_wechat_accounts(store, manager.installation, db)]
    for row in rows:
        row["key"] = row["platform"] + ":" + row["account"]
    # One stable total order so a cursor page can neither repeat nor skip an account.
    return sorted(rows, key=lambda row: row["key"])


def _records(store):
    grouped = {}
    for record in ReplyAuthorizations(store).records():
        scope = record["scope"]
        grouped.setdefault((scope["platform"], scope["account_identity"]), []).append(record)
    for rows in grouped.values():
        rows.sort(key=lambda record: record["id"])
    return grouped


def _locate(rows, platform, account):
    for row in rows:
        if row["platform"] == platform and row["account"] == account:
            return row
    _stop("reply_quota_unregistered",
          "该账号尚未完成核验登记，不能修改回复额度；请先在对应平台核验并保存账号。")


def _scope_location(manager, scope_id, account, db):
    """Resolve an opaque scope from durable records, never from page fields."""
    grouped = _records(manager.store)
    for row in accounts(manager, db):
        for record in grouped.get((row["platform"], row["account_identity"]), []):
            if record["id"] != scope_id:
                continue
            if row["account"] != account:
                _stop("edit_conflict", "账号已改变，请重新读取回复额度。")
            return row, record
    _stop("reply_quota_unregistered",
          "该会话尚未登记或所属账号未完成核验，不能修改额度。")


def _authorization(record, allow_all, eligible, kind, exclusion):
    label = "群" if kind == "group" else "好友"
    if exclusion["excluded"]:
        # A single-conversation exclusion wins over every authorization state, so the
        # page never suggests the conversation is currently replying.
        return f"已排除：已停用该{label}，不会回复；解除后只回应解除之后的新消息"
    if eligible:
        return "当前有效：已授权回复该会话"
    if allow_all:
        # An offline allow-all policy is not a revocation: it needs a running
        # connector to verify membership before it can reply.
        return f"允许所有{label}已开启；平台运行并按关系核验后生效，这不是撤销"
    if not record["enabled"]:
        return "已撤销：不会回复该会话（仍可调整任何额度）"
    if record.get("member_present", True) is False:
        return "已勾选，但该关系已离开或未通过最近核验；重新核实后从新消息继续"
    return "当前未生效：等待平台核验"


def _window(store, participation, scope, policy, token, db):
    window = store.state(Participation.window_key(scope), {}, db)
    run = store.state(CORE_RUN + scope.platform, {}, db)
    settings = policy["settings"]
    limit, decision_limit = settings["group_session_replies"], settings["group_session_decisions"]
    spent, decisions = Participation.spent(window), window.get("decisions", 0)
    now = participation.now()
    current = (bool(window) and window.get("status") == "active" and window.get("expires", 0) > now
               and spent < limit and decisions < decision_limit
               and token is not None and window.get("permission") == token
               and run.get("status") == "active" and window.get("generation") == run.get("generation"))
    if current:
        note = "当前阶段进行中"
    elif not window:
        note = "尚无群参与阶段"
    elif window.get("status") != "active":
        note = REASONS.get(window.get("reason"), "阶段已结束")
    elif window.get("expires", 0) <= now:
        note = "已超过阶段时长，阶段结束"
    elif spent >= limit:
        note = "本阶段回复数已用完，当前不再接话"
    elif decisions >= decision_limit:
        note = "本阶段决策次数已用完，当前不再接话"
    elif token is None or window.get("permission") != token:
        note = "参与授权已改变，该阶段不再有效"
    else:
        note = "属于已结束或已更换的运行，仅作历史记录"
    return {"current": current, "note": note, "status": window.get("status"),
            "reason": window.get("reason"), "id": window.get("id"),
            "started": window.get("started"), "expires": window.get("expires"),
            "reply_limit": limit, "decision_limit": decision_limit,
            "decisions": decisions, "used": spent,
            "confirmed": window.get("confirmed", 0), "reserved": window.get("reserved", 0),
            "uncertain": window.get("uncertain", 0)}


def _row(store, participation, policies, permissions, entry, record, db):
    scope = ReplyScope(**record["scope"])
    policy = policies.effective(scope, db)
    token = permissions.version(scope, db=db)
    exclusion = ReplyExclusions(store).record(scope, db=db)
    allow_all = bool(audience_read(
        store, Audience(scope.platform, scope.account_identity, scope.kind), db)["enabled"])
    name = entry["names"].get(scope.target_id)
    kind = "群" if scope.kind == "group" else "私聊"
    row = {"scope_id": scope.id, "kind": scope.kind, "target": scope.target_id, "name": name,
           "label": kind + " · " + (name or scope.target_id),
           # authorized = currently effective eligibility, not just the stored tick.
           "authorized": token is not None, "manual": record["enabled"],
           "member_present": record.get("member_present", True) is not False,
           "allow_all": allow_all,
           "excluded": exclusion["excluded"], "exclusion_version": exclusion["version"],
           "excluded_since": exclusion["since"], "reply_from": exclusion["not_before"],
           "status": _authorization(record, allow_all, token is not None, scope.kind, exclusion),
           "registered": scope.target_id in entry["targets"],
           "account": entry["account"], "platform": entry["platform"],
           "settings": dict(policy["settings"]), "inherited": policy["inherited"],
           "version": policy["version"],
           "fields": list(PRIVATE_FIELDS if scope.kind == "private" else GROUP_FIELDS)}
    if scope.kind == "private":
        row["limit"] = policy["settings"]["private_daily_replies"]
        try:
            bucket = participation.usage(scope, db)[1]
        except AssistantError as error:
            if error.code != "reply_quota_migration_required":
                raise
            # Never show a zero-filled day; the old ledger needs a human check.
            row.update(usage=None, used=None,
                       usage_error="今日已用量需要复核旧账本，暂不显示，也不会清零。")
        else:
            row["usage"] = {key: bucket.get(key, 0) for key in ("confirmed", "reserved", "uncertain")}
            row["used"] = Participation.spent(bucket)
    else:
        row["window"] = _window(store, participation, scope, policy, token, db)
    return row


def _counts(store, records, permissions, db):
    manual = sum(1 for record in records if record["enabled"])
    eligible = sum(1 for record in records
                   if permissions.version(ReplyScope(**record["scope"]), db=db) is not None)
    excluded = sum(1 for record in records
                   if ReplyExclusions(store).record(ReplyScope(**record["scope"]), db=db)["excluded"])
    return {"registered": len(records), "manual": manual, "eligible": eligible, "excluded": excluded}


def summary(manager, request, after):
    """One bounded page of saved accounts with their defaults and the rules behind them."""
    manager.authority.require(request, administrator=True)
    _cursor(after)
    store = manager.store
    policies, participation = ReplyPolicies(store), _participation(manager)
    permissions = ReplyAuthorizations(store)
    day, zone = _day(store, participation)
    grouped = _records(store)
    rows = accounts(manager)
    start = 0
    if after:
        found = next((index + 1 for index, row in enumerate(rows) if row["key"] == after), None)
        if found is None:
            _stop("reply_quota_unknown_account", "账号分页位置已失效，请重新读取回复额度。")
        start = found
    page = rows[start:start + ACCOUNT_PAGE]
    items = []
    with store.db() as db:
        for entry in page:
            records = grouped.get((entry["platform"], entry["account_identity"]), [])
            policy = policies.get(entry["platform"], entry["account_identity"], db)
            items.append({"key": entry["key"], "platform": entry["platform"], "account": entry["account"],
                          "label": entry["label"], "selected": entry["selected"],
                          "version": policy["version"], "settings": dict(policy["settings"]),
                          **_counts(store, records, permissions, db),
                          "allow_all": {kind: bool(audience_read(store, Audience(
                              entry["platform"], entry["account_identity"], kind), db)["enabled"])
                              for kind in ("private", "group")},
                          "platform_running": store.state(
                              CORE_RUN + entry["platform"], {}, db).get("status") == "active"})
    return {"accounts": items, "total": len(rows), "offset": start,
            "next": page[-1]["key"] if start + ACCOUNT_PAGE < len(rows) else None,
            "bounds": {key: list(value) for key, value in BOUNDS.items()},
            "defaults": dict(DEFAULTS), "fields": dict(FIELDS),
            "private_fields": list(PRIVATE_FIELDS), "group_fields": list(GROUP_FIELDS),
            "timezone": zone, "day": day, "quota_scope": "contact_calendar_day", "billing": False}


def scopes(manager, request, platform, account, after):
    """One bounded page of registered conversations with their live usage."""
    manager.authority.require(request, administrator=True)
    _platform(platform)
    _identity(account)
    _cursor(after)
    store = manager.store
    participation, policies = _participation(manager), ReplyPolicies(store)
    permissions = ReplyAuthorizations(store)
    grouped = _records(store)
    # Reads join one database unit of work but deliberately do not take the write
    # lock: a final platform send holds it while it talks to the platform, and a
    # page must not wait behind that. Writes re-check every version under the lock.
    with store.db() as db:
        entry = _locate(accounts(manager, db), platform, account)
        records = grouped.get((platform, entry["account_identity"]), [])
        start = 0
        if after:
            found = next((index + 1 for index, record in enumerate(records) if record["id"] == after), None)
            if found is None:
                _stop("reply_quota_unknown_scope", "分页位置已失效，请重新读取回复额度。")
            start = found
        page = records[start:start + PAGE]
        items = [_row(store, participation, policies, permissions, entry, record, db)
                 for record in page]
        back = max(0, start - PAGE)
        return {"platform": platform, "account": account, "label": entry["label"],
                "items": items, "total": len(records), "offset": start,
                "next": page[-1]["id"] if start + PAGE < len(records) else None,
                "previous": None if start == 0 else (records[back - 1]["id"] if back else "")}


def _audit(manager, request, operation, resource, before, after, db):
    manager.repository.audit(request.actor, operation, resource, before, after, db=db)


def _saved(store, participation, entry, record, db, *, changed):
    row = _row(store, participation, ReplyPolicies(store), ReplyAuthorizations(store), entry, record, db)
    return {**row, "changed": changed}


def account_save(manager, request, platform, account, changes, expected):
    """Account defaults stay editable offline; usage, authorization and lifecycle do not change."""
    _platform(platform)
    _identity(account)
    _limits(changes, DEFAULTS)
    if expected is not None and not isinstance(expected, str):
        _stop("reply_quota_invalid", "账号默认额度版本无效，请重新读取。")
    store = manager.store
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        manager.authority.require(request, administrator=True)
        entry = _locate(accounts(manager, db), platform, account)
        policies = ReplyPolicies(store)
        before = policies.get(platform, entry["account_identity"], db)["version"]
        saved = policies.update(platform, entry["account_identity"], dict(changes), expected, request.actor)
        _audit(manager, request, "desktop_reply_quota_account", platform + ":" + account, before,
               saved["version"], db)
        return {"platform": platform, "account": account, "version": saved["version"],
                "settings": dict(saved["settings"]), "changed": saved["version"] != before}


def scope_save(manager, request, scope_id, account, expected, changes):
    """A per-conversation override; a refusal keeps the stored policy untouched."""
    _identity(scope_id)
    _identity(account)
    if not isinstance(expected, str):
        _stop("reply_quota_invalid", "会话额度版本无效，请重新读取。")
    store = manager.store
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        manager.authority.require(request, administrator=True)
        entry, record = _scope_location(manager, scope_id, account, db)
        participation, policies = _participation(manager), ReplyPolicies(store)
        scope = ReplyScope(**record["scope"])
        current = policies.effective(scope, db)
        _limits(changes, current["settings"])
        if current["version"] != expected:
            _stop("edit_conflict", "会话额度或账号默认已改变，请重新读取后再保存。")
        wanted = {key: value for key, value in changes.items() if value != current["settings"][key]}
        if not wanted and not current["inherited"]:
            return _saved(store, participation, entry, record, db, changed=False)
        # ReplyPolicies.update_object stores an override even when the numbers equal
        # the account default, so saving an inheriting conversation on purpose pins
        # the submitted fields: a later account default change no longer moves it.
        saved = policies.update_object(scope, wanted or dict(changes), expected, request.actor)
        _audit(manager, request, "desktop_reply_quota_scope", scope_id, expected, saved["version"], db)
        return _saved(store, participation, entry, record, db, changed=True)


def scope_reset(manager, request, scope_id, account, expected):
    """Drop the override so the conversation inherits its account default again."""
    _identity(scope_id)
    _identity(account)
    if not isinstance(expected, str):
        _stop("reply_quota_invalid", "会话额度版本无效，请重新读取。")
    store = manager.store
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        manager.authority.require(request, administrator=True)
        entry, record = _scope_location(manager, scope_id, account, db)
        participation, policies = _participation(manager), ReplyPolicies(store)
        scope = ReplyScope(**record["scope"])
        current = policies.effective(scope, db)
        # The version check comes first: an inheriting scope is still a scope whose
        # page was read at some version, and a stale page must not report success.
        if current["version"] != expected:
            _stop("edit_conflict", "会话额度或账号默认已改变，请重新读取后再保存。")
        if current["inherited"]:
            return _saved(store, participation, entry, record, db, changed=False)
        saved = policies.update_object(scope, {}, expected, request.actor, restore=True)
        _audit(manager, request, "desktop_reply_quota_scope_restore", scope_id, expected,
               saved["version"], db)
        return _saved(store, participation, entry, record, db, changed=True)
