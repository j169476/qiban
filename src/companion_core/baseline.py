"""Explicitly shared, human-maintained character settings for every reply.

This is a small fixed context, separate from ranked interaction memories.
Publishing is an authenticated administrative operation; unknown imported data
is never made public merely because it happens to be called a foundation.
"""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from companion_core.runtime_keys import RUN_KEYS

from .errors import AssistantError

STATE = "shared-baseline"
MAX_CHARS = 65_536
_EDIT = ContextVar("baseline_manual_edit", default=frozenset())
GUIDANCE = (
    "persona 与 foundation_memories 是用户维护的共用人物设定，公开与私聊使用同一份。"
    "它们描述角色和明确设定，不把口吻示例当成实际经历。"
    "交流经历、知识、兴趣、情绪和好感继续按当前资料学习；不能修改共用人物卡或基础记忆。"
    "共用设定不授予管理或跨账号读取权限；各用户的交流资料仍按实际身份与会话范围使用。"
)


def enabled(store):
    return (store.state(STATE, {}) or {}).get("mode") == "shared"


def identifiers(persona):
    values = persona.get("foundation_memories", [])
    if (not isinstance(values, list) or len(values) > 100 or not all(isinstance(v, str) for v in values)
            or len(set(values)) != len(values)):
        raise AssistantError("baseline_invalid", "基础记忆清单无效")
    return tuple(values)


@contextmanager
def manual_edit(store):
    """Trusted admin call sites only, after session authentication."""
    token = _EDIT.set(_EDIT.get() | {id(store)})
    try:
        yield
    finally:
        _EDIT.reset(token)


def guard_write(store, kind, item_id, data):
    if kind not in {"persona", "memories"} or not enabled(store):
        return
    protected = kind == "persona" or item_id in identifiers(store.read("persona")["data"])
    if not protected:
        return
    if id(store) not in _EDIT.get():
        raise AssistantError("baseline_locked", "共用人物卡和基础记忆仅允许已认证管理端人工修改")
    if kind == "memories" and data.get("source", {}).get("type") != "user_instruction":
        raise AssistantError("baseline_source", "基础记忆必须保持明确的用户设定来源")


def validate_manual_edit(store):
    if enabled(store):
        preview(store)


def _records(store, persona=None):
    card = persona or store.read("persona")
    return [("persona", None, card), *(("memories", key, store.read("memories", key))
                                      for key in identifiers(card["data"]))]


def _bounded(records):
    if len(json.dumps([item[2]["data"] for item in records], ensure_ascii=False)) > MAX_CHARS:
        raise AssistantError("baseline_limit", "共用设定超过 65536 字符，请在管理端精简；未截断或替换人设")


def preview(store):
    """Only IDs and immutable revisions; callers separately display the content."""
    from .governance import GovernanceRepository, resource_id

    repository = GovernanceRepository(store)
    records = _records(store)
    _bounded(records)
    pins = []
    for kind, identifier, item in records:
        policy = repository.policy(resource_id(kind, identifier))
        if policy is None or policy.sources or policy.source_versions:
            raise AssistantError("baseline_source", "共用设定需要明确人工资料，不能直接公开派生的交流记忆")
        if kind == "memories" and item["data"].get("source", {}).get("type") != "user_instruction":
            raise AssistantError("baseline_source", "基础记忆必须有明确的用户设定来源")
        pins.append({"kind": kind, "id": identifier, "version": item["version"], "policy_version": policy.version})
    return {"pins": pins, "foundation_count": len(records) - 1}


def publish(store, expected, actor):
    """Trusted migration/admin operation after explicit publication authorization."""
    from .access import RecordPolicy
    from .governance import GovernanceRepository, resource_id

    if getattr(store, "backend", None) != "postgres":
        raise AssistantError("baseline_backend", "共用设定登记需要事务化 PostgreSQL 存储")
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if any((store.state(key, {}, db) or {}).get("status") == "active" for key in RUN_KEYS):
            raise AssistantError("baseline_active", "先暂停角色，再登记共用设定")
        current = preview(store)
        if current != expected:
            raise AssistantError("edit_conflict", "人物卡、基础记忆或权限在预览后已变化")
        repository = GovernanceRepository(store)
        for pin in current["pins"]:
            resource = resource_id(pin["kind"], pin["id"])
            repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "public"),
                                  pin["policy_version"], actor, db=db)
        store.set_state(STATE, {"mode": "shared", "version": 1}, db)
        # Earlier seed installation staged a *private* claim for first login.
        # This explicit publication supersedes it, not the user's authentication.
        claim = store.state("seed-owner-claim", {}, db) or {}
        if claim.get("pending"):
            store.set_state("seed-owner-claim", {**claim, "pending": False, "resolution": "shared_baseline"}, db)
        repository.audit(actor, "publish_baseline", "persona/@config", db=db)
        return {"shared": True, "foundation_count": current["foundation_count"], "role_started": False}


def sync_manual_card(store, before, actor):
    """A manual foundation-list edit publishes additions and retires removals."""
    if not enabled(store):
        return
    from .access import RecordPolicy
    from .governance import GovernanceRepository, resource_id

    repository = GovernanceRepository(store)
    after = store.read("persona")["data"]
    old, new = set(identifiers(before)), set(identifiers(after))
    current = preview(store)  # Also checks total size and user-instruction origin.
    for pin in current["pins"]:
        if pin["kind"] == "memories" and pin["id"] in new - old:
            resource = resource_id("memories", pin["id"])
            repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "public"),
                                  pin["policy_version"], actor)
    for identifier in old - new:
        resource = resource_id("memories", identifier)
        previous = repository.policy(resource)
        if previous:
            repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "unclassified", valid=False),
                                  previous.version, actor)


@dataclass
class Bundle:
    context: dict
    versions: dict
    pins: tuple
    check: object


def load(store, *, read=None):
    """Read all fixed material or fail; never silently truncate or switch persona."""
    if not enabled(store):
        return None
    from .access import AccessService, RequestAuthority
    from .governance import GovernanceRepository, resource_id, verify_record_versions

    state = store.state(STATE)
    repository = GovernanceRepository(store)
    authority = RequestAuthority(epoch=repository.epoch)
    access = AccessService(repository, authority)
    request = authority.issue(actor=None, conversation="shared-baseline", audience="public", lifetime=3600)
    reader = read or store.read

    def get(kind, identifier=None):
        resource = resource_id(kind, identifier)
        policy = repository.policy(resource)
        if policy is None or policy.classification != "public" or not access.can_read(request, resource):
            raise AssistantError("baseline_unavailable", "共用人物设定缺失、被撤销或尚未公开；未替换为默认人设")
        result = reader(kind, identifier)
        if result is None:
            raise AssistantError("baseline_unavailable", "当前请求不能读取完整共用人物设定")
        return result

    card = get("persona")
    records = [("persona", None, card)]
    records.extend(("memories", key, get("memories", key)) for key in identifiers(card["data"]))
    _bounded(records)
    versions = {(kind, identifier): item["version"] for kind, identifier, item in records}
    lease = access.lease(request, [resource_id(kind, identifier) for kind, identifier, _ in records])

    def check():
        if store.state(STATE) != state:
            raise AssistantError("context_changed", "共用设定登记已变化")
        access.revalidate(lease)
        verify_record_versions(store, versions)

    check()
    return Bundle({"persona": card["data"], "foundation_memories": [item for _, _, item in records[1:]],
                   "baseline_guidance": GUIDANCE}, versions, lease.versions, check)
