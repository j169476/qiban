"""Explicit deletion retains encrypted history and invalidates every old source pin."""
import secrets
from dataclasses import replace

from .errors import AssistantError
from .governance import GovernanceRepository, resource_id


def remove(store, kind, identifier, expected, actor):
    if kind not in store.profile.collections:
        raise AssistantError("invalid_delete", "只能删除普通集合资料")
    from . import baseline

    if kind == "memories" and identifier in baseline.identifiers(store.read("persona")["data"]):
        raise AssistantError("baseline_locked", "请先从共用人物卡移除这条基础记忆")
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT version,body FROM content_records WHERE kind=? AND id=? FOR UPDATE", (kind, identifier)).fetchone()
        if not row or row["version"] != expected:
            raise AssistantError("edit_conflict", "资料在预览后已经改变")
        repository = GovernanceRepository(store)
        resource = resource_id(kind, identifier)
        policy = repository.policy(resource)
        if policy:
            repository.set_policy(replace(policy, version=secrets.token_hex(16), valid=False), policy.version, actor)
        db.execute("INSERT INTO content_history(kind,id,version,body) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                   (kind, identifier, row["version"], row["body"]))
        db.execute("DELETE FROM content_records WHERE kind=? AND id=?", (kind, identifier))
        if kind == "input_images":
            db.execute("DELETE FROM input_image_blobs WHERE id=?", (identifier,))
        repository.audit(actor, "delete", resource, expected, None, db=db)
    return {"deleted": True, "history_retained": True, "derivatives_invalidated": True}
