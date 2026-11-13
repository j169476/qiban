"""Stop or re-enable replies for one saved, previously verified conversation.

This is the offline single-conversation control: it writes one
``companion_core.reply_exclusion`` record and nothing else. It does not pause the
account, stop the platform, change the manual list, change "allow all" for the
account, change any reply quota, contact a connector or call a model, so every
other conversation keeps replying exactly as before.

Only a scope that the durable registration records still prove can be touched; a
page supplied account or scope id is never trusted on its own. Lifting an
exclusion removes only the exclusion: the conversation keeps whatever
authorization and relationship it already had, and it will only answer messages
the platform timestamped after the lift.

Trusted account location and paging are shared with ``desktop_reply_quota`` so
both pages resolve the same registration evidence and the same 20-row cursors.
"""
from __future__ import annotations

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_exclusion import ReplyExclusions
from companion_core.reply_policy import ReplyPolicies

from .desktop_reply_quota import _counts, _identity, _participation, _records, _row, _scope_location


def _stop(code, message):
    raise AssistantError(code, message)


def _expected(value):
    if value is not None and not isinstance(value, str):
        _stop("reply_exclusion_invalid", "排除状态版本无效，请重新读取。")
    return value


def _hint(row, excluded):
    """Say what actually happened; lifting an exclusion does not restore a permit."""
    label = "群" if row["kind"] == "group" else "私聊"
    if excluded:
        return (f"已停用此{label}：该会话不会回复，其他会话、账号与平台保持原状；"
                "解除后只回应解除之后的新消息。")
    if row["authorized"]:
        return ("已解除排除：按原有授权继续，只会回应解除之后的新消息；"
                "排除期间的消息不会补回复。")
    if row["allow_all"]:
        return (f"已解除排除：允许所有{label}已开启，但要平台运行并按关系核验后才可能回复；"
                "解除排除不等于已恢复回复。")
    if not row["manual"]:
        return "已解除排除：该会话仍是已撤销状态，需要连接器核验后重新授权才会回复。"
    if not row["member_present"]:
        return "已解除排除：该关系尚未核验回来，重新核实后只回应新消息；当前尚待核验。"
    return "已解除排除：当前仍需平台核验，尚待核验；解除排除不等于已恢复回复。"


def _apply(manager, request, scope_id, account, expected, excluded):
    _identity(scope_id)
    _identity(account)
    _expected(expected)
    store = manager.store
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        manager.authority.require(request, administrator=True)
        entry, record = _scope_location(manager, scope_id, account, db)
        scope = ReplyScope(**record["scope"])
        exclusions = ReplyExclusions(store)
        previous = exclusions.record(scope, db=db)
        saved = exclusions.update(scope, excluded=excluded, expected_version=expected,
                                  actor=request.actor, db=db)
        changed = saved["version"] != previous["version"]
        if changed:
            manager.repository.audit(
                request.actor,
                "desktop_reply_exclusion" if excluded else "desktop_reply_exclusion_lift",
                scope_id, previous["version"], saved["version"], db=db)
        permissions = ReplyAuthorizations(store)
        row = _row(store, _participation(manager), ReplyPolicies(store), permissions, entry, record, db)
        # The account summary is recomputed from durable records in the same unit of
        # work, so the page can refresh its counts without re-reading everything.
        counts = _counts(store, _records(store).get((entry["platform"], entry["account_identity"]), []),
                         permissions, db)
    return {**row, "changed": changed, "counts": counts, "message": _hint(row, excluded)}


def exclude(manager, request, scope_id, account, expected):
    """Stop replying to one registered conversation; never pause the account."""
    return _apply(manager, request, scope_id, account, expected, True)


def restore(manager, request, scope_id, account, expected):
    """Remove the exclusion only; the original authorization rules decide the rest."""
    return _apply(manager, request, scope_id, account, expected, False)
