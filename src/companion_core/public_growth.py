"""Sanitized diagnostics and explicit, transactional recovery of public growth.

Recovery starts from shipped neutral defaults. Unreadable derived text is never
copied or relabelled public. The old encrypted state is retained for admin review.
"""
import hashlib
import json
import secrets
from copy import deepcopy

from .access import RecordPolicy
from .errors import AssistantError
from .governance import GovernanceRepository

RESOURCE = "partition/public-growth"
MESSAGES = {
    "policy_missing": "公开成长状态缺少来源登记，请在管理页预览重建。",
    "source_changed": "公开成长状态的来源版本已变化，请在管理页复核并预览重建。",
    "source_denied": "公开成长状态的来源授权已失效，请在管理页复核并预览重建。",
    "invalid": "公开成长状态未通过完整性校验，请在管理页预览重建。",
    "validation_failed": "公开成长来源验证未完成，请查看管理页诊断。",
}


def require_readable(workspace, result, policy):
    cause = None
    if policy is None:
        cause = "policy_missing"
    elif (not isinstance(result, dict) or set(result) != {"data", "version"}
          or workspace._version(result["data"]) != result["version"]):
        cause = "invalid"
    else:
        try:
            workspace.access.lease(workspace.request, [RESOURCE])
        except AssistantError as exc:
            if exc.code != "access_denied":
                raise
            # The access layer emits fixed safe messages; never expose graph IDs.
            cause = ("source_changed" if str(exc) == "来源版本已改变或不存在" else
                     "source_denied" if str(exc) == "请求的资料不存在或不可在当前会话使用" else
                     "validation_failed")
    if cause:
        raise AssistantError("public_growth_" + cause, MESSAGES[cause])


def fingerprint(store):
    repo = GovernanceRepository(store)
    policy = repo.policy(RESOURCE)
    # Include all permission metadata: a change after preview requires a new preview.
    with store.db() as db:
        policies = [dict(row) for row in db.execute("SELECT * FROM governance_policies ORDER BY resource")]
    raw = json.dumps([store.state("public-growth"), policy.version if policy else None, policies],
                     sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def preview(store):
    from .public_workspace import PublicWorkspace

    workspace = PublicWorkspace(store)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            workspace.read("growth")
            reason = "readable"
        except AssistantError as exc:
            if not exc.code.startswith("public_growth_"):
                raise
            reason = exc.code
        return {"expected": fingerprint(store), "reason": reason,
                "data": deepcopy(store.profile.defaults["growth"]), "basis": "neutral_defaults",
                "preserves_original": True, "requires_explicit_start": True}


def rebuild(store, expected, actor):
    from .public_workspace import PublicWorkspace

    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if fingerprint(store) != expected:
            raise AssistantError("edit_conflict", "公开成长状态或来源在预览后变化，请重新预览")
        original = store.state("public-growth")
        repo = GovernanceRepository(store)
        old_policy = repo.policy(RESOURCE)
        archive = "public-growth-recovery:" + secrets.token_hex(16)
        store.set_state(archive, {"original": original,
                                  "policy_version": old_policy.version if old_policy else None}, db)
        data = deepcopy(store.profile.defaults["growth"])
        store.profile.validate("growth", data)
        # This is deliberately an administrative write: old derived evidence
        # remains revoked; rebuilding must never revive old snapshots.
        store.set_state("public-growth", {"data": data, "version": PublicWorkspace._version(data)}, db)
        repo.set_policy(RecordPolicy(RESOURCE, secrets.token_hex(16), "public"),
                        old_policy.version if old_policy else None, actor, db=db)
        repo.audit(actor, "public_growth_rebuild", RESOURCE, expected, fingerprint(store), db=db)
        return {"rebuilt": True, "archive": archive, "requires_explicit_start": True}
