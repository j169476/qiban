"""Bounded, read-only walk of a whole source graph, plus a stable state digest.

This is the same walk the record-sharing page already used: one record's complete
source closure, with a cycle, an over-deep chain, a missing policy, a rotated pin,
a revoked node, an expired node, a quarantined classification or a truncated
budget reported as an issue instead of being silently accepted. The core
``AccessService`` refuses exactly these graphs, so a page must not claim that a
record's sources are healthy when they are not.

Two callers need the same walk for different questions:

* ``record_sharing`` asks "can this grant/classification take effect?" and only
  needs the human-readable report;
* ``relation_claims`` must additionally prove that nothing in the closure moved
  between a preview and its commit, so it also needs the *exact* state of every
  visited node (version, validity-at-check-time, expiry-at-check-time,
  classification and pins), not only the issue text. Two different rotations of
  the same source produce identical issue wording, so binding issue strings alone
  would let a stale preview commit.

The report body contains no read timestamp: a clock that advances without any
change must not invalidate a preview.
"""
from __future__ import annotations

import hashlib
import json
import time

from .provenance import reserved


def policy_state(policy):
    """Everything a check must keep unchanged, in a stable order.

    Shared with the record-sharing page so one definition decides what "the same
    policy" means. ``expires_at`` is the stored value, never a read timestamp.
    """
    return [policy.resource, policy.version, policy.classification, sorted(policy.owners),
            policy.conversation, policy.audience, list(policy.sources),
            [list(pin) for pin in policy.source_versions], bool(policy.valid), policy.expires_at]


def _state_digest(policies, now):
    """Exact per-node state, in a stable order; no read timestamps.

    The stored expiry is bound as well as the expiry *at check time*: an edit that
    moves an expiry without bumping the policy version is a change, and a source
    crossing its expiry between a preview and its commit is a change too.
    """
    items = []
    for key in sorted(policies):
        policy = policies[key]
        if policy is None:
            items.append([key, None])
            continue
        items.append([key, policy_state(policy),
                      policy.expires_at is not None and policy.expires_at <= now])
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def closure(repository, limits, resource, *, root=None):
    """Report and stable digest for one record's complete source closure.

    ``root`` substitutes the root's own policy only (used to describe the state a
    preview would produce); every source is always read as it really is.
    """
    deadline = time.monotonic() + limits.seconds
    now = repository.clock()
    report_policy = repository.policy(resource) if root is None else root
    if report_policy is None:
        return {"report": {"ok": False, "nodes": 0, "depth": 0, "limited": False, "total_issues": 1,
                           "issues": [{"resource": resource, "reason": "policy_missing",
                                       "detail": "资料的权限记录不存在，请先检查恢复"}]},
                "state": _state_digest({resource: None}, now), "complete": True}
    policies, issues = {resource: report_policy}, []
    seen, active, heights = set(), set(), {}
    stack, edges, depth, truncated = [(resource, False, 0)], 0, 0, False
    complete = True
    while stack:
        if time.monotonic() > deadline or len(policies) > limits.nodes or edges > limits.edges:
            truncated = True
            complete = False
            break
        key, exiting, level = stack.pop()
        if exiting:
            active.discard(key)
            seen.add(key)
            heights[key] = 1 + max((heights.get(child, 0)
                                    for child in policies[key].sources), default=0)
            depth = max(depth, level + heights[key])
            continue
        if key in seen:
            # A shared source can be reached again through a longer path.
            # Reuse its subtree height, not its first visitation depth.
            path_depth = level + heights.get(key, 1)
            depth = max(depth, path_depth)
            if path_depth > limits.depth:
                issues.append({"resource": key, "reason": "depth",
                               "detail": "来源层级超过核对上限，请先整理资料"})
            continue
        # A node still on the walk's own path is a genuine cycle. The core denies
        # it, so it must appear here as well.
        if key in active:
            issues.append({"resource": key, "reason": "cycle",
                           "detail": "来源关系出现循环，读取会被拒绝"})
            continue
        if level + 1 > limits.depth:
            complete = False
            issues.append({"resource": key, "reason": "depth",
                           "detail": "来源层级超过核对上限，请先整理资料"})
            continue
        depth = max(depth, level + 1)
        policy = policies.get(key)
        if policy is None:
            policy = policies[key] = repository.policy(key)
        if policy is None:
            issues.append({"resource": key, "reason": "source_missing",
                           "detail": "引用的来源没有权限记录"})
            seen.add(key)
            continue
        if not policy.valid:
            issues.append({"resource": key, "reason": "revoked", "detail": "资料或来源已被停用"})
        if policy.expires_at is not None and policy.expires_at <= now:
            issues.append({"resource": key, "reason": "expired", "detail": "资料或来源已经过期"})
        if policy.classification in ("secret", "unclassified"):
            issues.append({"resource": key, "reason": "quarantined",
                           "detail": "资料或来源处于保密或未分类的隔离状态"})
        if reserved(key) and repository.provenance_target(key) is None:
            issues.append({"resource": key, "reason": "reserved_unregistered",
                           "detail": "历史来源注册不完整，请先检查恢复"})
        pins = dict(policy.source_versions)
        if len(pins) != len(policy.source_versions) or set(pins) != set(policy.sources):
            complete = False
            issues.append({"resource": key, "reason": "source_pins_incomplete",
                           "detail": "来源版本记录不完整，请先检查恢复"})
            seen.add(key)
            continue
        # Duplicate edges consume budget but are not a second path: only the
        # distinct children are pushed, as ``AccessService._walk`` does.
        children = tuple(dict.fromkeys(policy.sources))
        for source in children:
            current = policies.get(source)
            if current is None:
                current = policies[source] = repository.policy(source)
            if current is None or current.version != pins[source]:
                issues.append({"resource": source, "reason": "source_changed",
                               "detail": "引用的来源版本已变化"})
        edges += len(policy.sources)
        active.add(key)
        stack.append((key, True, level))
        # Every child is pushed: an edge back into the active path is a cycle.
        stack.extend((source, False, level + 1) for source in reversed(children))
    if truncated:
        issues.append({"resource": resource, "reason": "budget",
                       "detail": "来源关系超过本次核对上限，请先整理资料"})
    return {"report": {"ok": not issues, "nodes": len(policies), "depth": depth,
                       "issues": issues[:200], "total_issues": len(issues),
                       "limited": len(issues) > 200},
            "state": _state_digest(policies, now), "complete": complete}


def digest(check):
    """Fingerprint of one check: the report wording *and* the exact node state."""
    return hashlib.sha256(json.dumps([check["report"], check["state"]], sort_keys=True)
                          .encode()).hexdigest()
