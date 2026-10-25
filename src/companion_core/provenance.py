"""Immutable evidence for two routinely updated aggregates, with live revocation.

Only trusted synchronous commit paths enter routine_update. Ordinary writes,
including an identical administrative reclassification, rotate the live scope.
Virtual records carry permission metadata only; they never contain user text.
"""
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from hashlib import sha256

from .errors import AssistantError

PREFIX = "provenance/"
_WRITE = object()
_ROUTINE = ContextVar("aggregate_provenance_update", default=None)


def mutable(resource):
    return resource.startswith("relationships/") or resource == "partition/public-growth"


def reserved(resource):
    return resource.startswith(PREFIX)


def scope_id(resource):
    return PREFIX + "scope/" + sha256(resource.encode()).hexdigest()


def permissions(policy):
    return (policy.classification, policy.owners, policy.conversation, policy.audience,
            policy.valid, policy.expires_at)


def is_routine(store, resource):
    return _ROUTINE.get() == (id(store), resource)


@contextmanager
def routine_update(store, resource):
    if not mutable(resource):
        raise AssistantError("provenance_scope", "该资料不支持成长来源快照")
    token = _ROUTINE.set((id(store), resource))
    try:
        yield
    finally:
        _ROUTINE.reset(token)


def sync_scope(repository, previous, policy, db, *, force=False):
    if not mutable(policy.resource):
        return
    scope = repository.policy(scope_id(policy.resource))
    if scope is None:
        return
    if (not force and is_routine(repository.store, policy.resource) and previous is not None
            and permissions(previous) == permissions(policy)):
        return
    updated = replace(policy, resource=scope.resource, version=secrets.token_hex(16),
                      sources=(), source_versions=())
    repository.set_policy(updated, scope.version, "aggregate-scope", db=db, _provenance=_WRITE)


def invalidate_state(repository, db):
    """A direct/admin public-growth state write must invalidate old evidence too."""
    resource = "partition/public-growth"
    missing = repository.store.state("public-growth") is None
    if missing or not is_routine(repository.store, resource):
        policy = repository.policy(resource)
        if policy:
            sync_scope(repository, policy, policy, db, force=missing)


def _register(repository, policy, target, kind, origin, db):
    repository.set_policy(policy, None, "aggregate-snapshot", db=db, _provenance=_WRITE)
    db.execute("INSERT INTO governance_provenance VALUES(?,?,?,?)", (policy.resource, target, kind, origin))


def reduce_pins(access, lease):
    """Remove only edges already implied by an identical-version path in this DAG.

    Every original root remains in the verified closure. This is graph reduction,
    not history truncation or a permission snapshot for additional record types.
    """
    import time

    deadline = time.monotonic() + access.limits.seconds
    reader = access._reader(lease.roots, known=[key for key, _ in lease.versions], deadline=deadline)
    dict_versions = dict(lease.versions)
    roots, seen, redundant = set(lease.roots), set(), set()
    stack = []
    for key in roots:
        policy = reader.repository.policy(key)
        if policy is None or policy.version != dict_versions[key]:
            raise AssistantError("context_changed", "归并时来源已经变化")
        stack.extend(policy.sources)
    while stack:
        if time.monotonic() > deadline:
            raise AssistantError("access_denied", "来源图归并超出处理预算")
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        if key in roots:
            redundant.add(key)
        policy = reader.repository.policy(key)
        if policy is None or policy.version != dict_versions[key]:
            raise AssistantError("context_changed", "归并时来源已经变化")
        stack.extend(policy.sources)
    return tuple((key, dict_versions[key]) for key in lease.roots if key not in redundant)


def freeze(access, lease):
    """Freeze direct evidence, preserving the entire graph in the live lease."""
    repository = access.repository
    if getattr(repository.store, "backend", None) != "postgres":
        # The editable legacy YAML/SQLite store cannot atomically commit payload
        # and metadata. Do not imply that it provides this new service guarantee.
        if any(mutable(key) for key, _ in lease.versions):
            raise AssistantError("provenance_backend", "成长来源快照需要事务化 PostgreSQL 核心")
        versions = dict(lease.versions)
        return lease, tuple((key, versions[key]) for key in lease.roots)
    with repository.store.lock, repository.store.db() as db:
        if getattr(db, "dialect", "sqlite") == "postgres":
            db.execute("BEGIN IMMEDIATE")
        access.revalidate(lease)
        memo = {}

        def snapshot(resource, version):
            # The already-validated DAG may be deeper than Python's call stack.
            # Only these two aggregate types are converted, exactly as before.
            pending = [(resource, version, None)]
            while pending:
                key, expected, policy = pending.pop()
                if key in memo:
                    continue
                if not mutable(key):
                    memo[key] = (key, expected)
                    continue
                if policy is None:
                    policy = repository.policy(key)
                    if policy is None or policy.version != expected:
                        raise AssistantError("context_changed", "汇总资料在冻结前已变化")
                    pending.append((key, expected, policy))
                    pins = dict(policy.source_versions)
                    pending.extend((child, pins[child], None) for child in policy.sources if child not in memo)
                    continue
                scope = repository.policy(scope_id(key))
                if scope is None:
                    scope = replace(policy, resource=scope_id(key), version=secrets.token_hex(16),
                                    sources=(), source_versions=())
                    _register(repository, scope, key, "scope", "", db)
                pins = tuple(sorted({scope.resource: scope.version,
                                     **dict(memo[child] for child in policy.sources)}.items()))
                identifier = PREFIX + "snapshot/" + sha256(
                    (key + "\n" + expected + "\n" + scope.version).encode()).hexdigest()
                frozen = replace(policy, resource=identifier, version=expected,
                                 sources=tuple(child for child, _ in pins), source_versions=pins)
                existing = repository.policy(identifier)
                if existing is None:
                    _register(repository, frozen, key, "snapshot", expected, db)
                elif existing != frozen or repository.provenance_target(identifier) != key:
                    raise AssistantError("provenance_invalid", "来源快照不可覆盖或冒用")
                memo[key] = (identifier, frozen.version)
            return memo[resource]

        versions = dict(lease.versions)
        pins = tuple(snapshot(key, versions[key]) for key in lease.roots)
        frozen = access.lease(lease.request, [key for key, _ in pins])
        pins = reduce_pins(access, frozen)
        reduced = access._lease(lease.request, [key for key, _ in pins], known=[key for key, _ in frozen.versions])
        if reduced.versions != frozen.versions:
            raise AssistantError("context_changed", "来源图归并不能改变完整验证集合")
        live = access._lease(lease.request, lease.roots, frozen.roots)
        return live, pins


def rebase(access, lease, resource, written_version, frozen_sources):
    """Accept exactly one committed local revision, never refreshed foreign reads."""
    access.authority.require(lease.request)
    policy = access.repository.policy(resource)
    if policy is None or policy.version != written_version:
        raise AssistantError("context_changed", "本轮写入已被另一入口替换")
    expected = dict(lease.versions)
    # Frozen roots were already included in this lease by freeze(). Verify all
    # prior nodes afresh, including any a routine write no longer points to.
    allowed = {**expected, resource: written_version}
    current = access._lease(lease.request, lease.roots, tuple(expected), known=list(expected))
    if any(allowed.get(key) != version for key, version in current.versions):
        raise AssistantError("context_changed", "本轮写入引入了未经冻结的来源")
    return replace(current, guards=tuple(expected))
