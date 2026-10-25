"""Deterministic disclosure rules, independent of platform, model and storage.

Callers obtain RequestContext from a trusted transport/session verifier. Neither
identity-link inference nor model output is an input to authorization. Filtering
must happen before ranking/aggregation; a frozen lease is rechecked before send.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Protocol

from .contracts import ActorRef, Event
from .errors import AssistantError

CLASSIFICATIONS = frozenset(("public", "conversation", "personal", "restricted", "secret", "unclassified"))
PURPOSES = frozenset(("reply", "exploration", "maintenance", "management"))


@dataclass(frozen=True)
class RequestContext:
    actor: str | None
    conversation: str
    audience: str
    recipients: frozenset[str]
    purpose: str
    expires_at: float
    session_id: str
    epoch: int
    administrator: bool = False
    _issuer: object = field(default=None, repr=False, compare=False)
    _signature: str = field(default="", repr=False, compare=False)


class RequestAuthority:
    """Owned by the service, never exposed through model/tool parameters.

    An issuer is not an authentication mechanism: adapters must verify their
    transport and the web server must verify its session *before* calling issue.
    Epoch/session callbacks let durable revocation invalidate in-flight requests.
    """
    def __init__(self, *, epoch=lambda: 0, session_active=lambda value: True, clock=time.time):
        self._seal = object()
        self._key = secrets.token_bytes(32)
        self.epoch, self.session_active, self.clock = epoch, session_active, clock

    def _signature(self, request):
        values = (request.actor, request.conversation, request.audience, sorted(request.recipients),
                  request.purpose, request.expires_at, request.session_id, request.epoch, request.administrator)
        return hmac.new(self._key, json.dumps(values, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()

    def issue(self, *, actor, conversation, audience, recipients=(), purpose="reply",
              session_id="", administrator=False, lifetime=300):
        if (audience not in ("private", "group", "public") or purpose not in PURPOSES
                or not isinstance(conversation, str) or not conversation or not 0 < lifetime <= 3600
                or (actor is not None and not isinstance(actor, ActorRef))
                or (administrator and (actor is None or purpose != "management"))):
            raise AssistantError("access_context", "可信请求范围无效")
        receiver_keys = frozenset(recipient.key for recipient in recipients)
        if audience == "private" and (actor is None or receiver_keys != {actor.key}):
            raise AssistantError("access_context", "私密回复的接收账号必须与已认证账号一致")
        request = RequestContext(actor.key if actor else None, conversation, audience, receiver_keys, purpose,
                                 self.clock() + lifetime, session_id, self.epoch(), administrator, self._seal)
        return replace(request, _signature=self._signature(request))

    def for_event(self, event: Event, *, purpose="reply"):
        return self.issue(actor=event.actor, conversation=event.thread, audience=event.visibility,
                          recipients=(event.actor,) if event.visibility == "private" else (), purpose=purpose)

    def require(self, request, *, administrator=False):
        if (not isinstance(request, RequestContext) or request._issuer is not self._seal
                or not hmac.compare_digest(request._signature, self._signature(request))
                or request.expires_at <= self.clock() or request.epoch != self.epoch()
                or (request.session_id and not self.session_active(request.session_id))
                or (administrator and not request.administrator)):
            raise AssistantError("access_denied", "请求身份、授权或管理会话已失效")


@dataclass(frozen=True)
class RecordPolicy:
    resource: str
    version: str
    classification: str = "unclassified"
    owners: frozenset[str] = frozenset()
    conversation: str = ""
    audience: str = "private"
    sources: tuple[str, ...] = ()
    valid: bool = True
    expires_at: float | None = None
    source_versions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if (self.classification not in CLASSIFICATIONS or not self.resource or not self.version
                or self.audience not in ("private", "group", "public")):
            raise AssistantError("record_policy", "资料权限标记无效")


@dataclass(frozen=True)
class AccessGrant:
    id: str
    grantee: str
    owner: str
    resources: frozenset[str]
    purposes: frozenset[str]
    basis: str
    evidence_id: str
    expires_at: float
    epoch: int
    revoked: bool = False

    def covers(self, request, policy, now):
        return (not self.revoked and self.epoch == request.epoch and self.expires_at > now
                and self.basis in ("owner_admin", "account_control") and bool(self.evidence_id)
                and request.actor == self.grantee and self.owner in policy.owners
                and policy.resource in self.resources and request.purpose in self.purposes
                and request.audience == "private" and request.recipients == {request.actor})


class AccessRepository(Protocol):
    def policy(self, resource: str) -> RecordPolicy | None: ...
    def grants(self, grantee: str) -> tuple[AccessGrant, ...]: ...


@dataclass(frozen=True)
class DisclosureLease:
    request: RequestContext
    versions: tuple[tuple[str, str], ...]
    nonce: str
    roots: tuple[str, ...] = ()
    guards: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphLimits:
    nodes: int = 10_000
    edges: int = 50_000
    depth: int = 4_096
    seconds: float = 2.0


class AccessService:
    def __init__(self, repository: AccessRepository, authority: RequestAuthority, *, max_sources=256, limits=None):
        self.repository, self.authority = repository, authority
        self.max_sources = max_sources
        self.limits = limits or GraphLimits()

    def _direct(self, request, policy, grants):
        from .provenance import reserved

        target = None
        if reserved(policy.resource):
            target = getattr(self.repository, "provenance_target", lambda _: None)(policy.resource)
            if target is None:
                return False
        now = self.authority.clock()
        if not policy.valid or (policy.expires_at is not None and policy.expires_at <= now):
            return False
        # Secrets cannot enter any model context, even for the administrator.
        # Unclassified legacy data is quarantined until an explicit review.
        if policy.classification in ("secret", "unclassified"):
            return False
        if policy.classification == "public":
            return True
        if policy.classification == "conversation":
            return policy.conversation == request.conversation and policy.audience == request.audience
        if (request.audience != "private" or request.recipients != {request.actor}
                or request.purpose not in ("reply", "maintenance", "management")):
            return False
        if policy.classification == "personal" and request.actor in policy.owners:
            return True
        # Only exact cross-account grants inspect the resource ID. Owner/public/
        # conversation checks use unchanged metadata and need no copied policy.
        # The live virtual registration above is still checked on every visit.
        if target is not None and grants:
            policy = replace(policy, resource=target)
        return any(grant.covers(request, policy, now) for grant in grants)

    def _walk(self, request, resources, grants, deadline):
        """Iterative, complete DAG validation. No authorization cache crosses checks."""
        versions, policies, heights, active = {}, {}, {}, set()
        edges = 0
        for root in resources:
            stack = [(root, False)]
            while stack:
                if time.monotonic() > deadline:
                    raise AssistantError("access_denied", "来源验证超出处理预算")
                key, exiting = stack.pop()
                if exiting:
                    policy = policies[key]
                    height = 1 + max((heights[source] for source in policy.sources), default=0)
                    if height > self.limits.depth:
                        raise AssistantError("access_denied", "来源验证超出深度预算")
                    heights[key] = height
                    active.remove(key)
                    versions[key] = policy.version
                    continue
                if key in active:
                    raise AssistantError("access_denied", "来源验证发现循环")
                if key in versions:
                    continue
                if key not in policies:
                    policies[key] = self.repository.policy(key)
                policy = policies[key]
                if policy is None or not self._direct(request, policy, grants):
                    raise AssistantError("access_denied", "请求的资料不存在或不可在当前会话使用")
                # Legacy flattened records remain readable; the graph edge budget
                # bounds them. Only new model inputs are limited to 256 roots.
                sources = tuple(dict.fromkeys(policy.sources))
                pins = dict(policy.source_versions)
                if len(pins) != len(policy.source_versions) or set(pins) != set(sources):
                    raise AssistantError("access_denied", "来源版本记录不完整")
                edges += len(policy.sources)
                if len(policies) > self.limits.nodes or edges > self.limits.edges:
                    raise AssistantError("access_denied", "来源验证超出图预算")
                for source in sources:
                    if source not in policies:
                        policies[source] = self.repository.policy(source)
                    if policies[source] is None or policies[source].version != pins[source]:
                        raise AssistantError("access_denied", "来源版本已改变或不存在")
                if len(policies) > self.limits.nodes:
                    raise AssistantError("access_denied", "来源验证超出节点预算")
                active.add(key)
                stack.append((key, True))
                stack.extend((source, False) for source in reversed(sources))
        return tuple(sorted(versions.items()))

    def can_read(self, request, resource):
        self.authority.require(request)
        try:
            self.lease(request, [resource])
            return True
        except AssistantError as exc:
            if exc.code != "access_denied":
                raise
            self.authority.require(request)
            return False

    def _reader(self, resources, *, known=None, deadline=None):
        snapshot = getattr(self.repository, "disclosure_snapshot", None)
        return (AccessService(snapshot(resources, self.limits.nodes, known=known, deadline=deadline), self.authority,
                              max_sources=self.max_sources, limits=self.limits)
                if snapshot else self)

    def lease(self, request, resources):
        resources = tuple(dict.fromkeys(resources))
        if len(resources) > self.max_sources:
            raise AssistantError("access_denied", "本轮直接引用超过上限")
        return self._lease(request, resources)

    def _lease(self, request, resources, guards=(), *, known=None):
        deadline = time.monotonic() + self.limits.seconds
        self.authority.require(request)
        grants = self.repository.grants(request.actor) if request.actor else ()
        roots = tuple(dict.fromkeys((*resources, *guards)))
        reader = self._reader(roots, known=known, deadline=deadline)
        versions = reader._walk(request, roots, grants, deadline)
        self.authority.require(request)
        if time.monotonic() > deadline:
            raise AssistantError("access_denied", "来源验证超出处理预算")
        return DisclosureLease(request, versions, secrets.token_hex(16), tuple(resources), tuple(guards))

    def revalidate(self, lease):
        self.authority.require(lease.request)
        current = self._lease(lease.request, lease.roots, lease.guards, known=[key for key, _ in lease.versions])
        if current.versions != lease.versions:
            raise AssistantError("context_changed", "资料或授权已改变，旧回复不可发送")

    def filter_before_ranking(self, request, candidates, resource_key):
        self.authority.require(request)
        return [item for item in candidates if self.can_read(request, resource_key(item))]

    def readable_snapshot(self, request, policies, grants):
        """Validate a catalog DAG once, including each root's exact graph budgets.

        Bitsets describe metadata reachability only, never an authorization cache.
        Leaf records use no retained bitset; a large independent library is cheap.
        """
        deadline = time.monotonic() + self.limits.seconds
        memo, identifiers, edge_ids = {}, {}, {}

        def bit(key):
            return 1 << identifiers.setdefault(key, len(identifiers))

        def leaf(key):
            return (bit(key), 0, 1)

        for root in policies:
            stack, active = [(root, False)], set()
            while stack:
                if time.monotonic() > deadline:
                    raise AssistantError("access_denied", "来源目录验证超出处理预算")
                key, exiting = stack.pop()
                if key in memo:
                    continue
                policy = policies.get(key)
                if exiting:
                    active.discard(key)
                    children = [memo.get(source) for source in policy.sources]
                    if any(child is None for child in children):
                        memo[key] = None
                        continue
                    nodes, edges, height = bit(key), 0, 1
                    for source, child in zip(policy.sources, children):
                        child = leaf(source) if child is True else child
                        nodes |= child[0]
                        edges |= child[1]
                        height = max(height, child[2] + 1)
                    # Edge identities include position so duplicate edges consume
                    # the processing budget too, without duplicating descendants.
                    for index in range(len(policy.sources)):
                        edges |= 1 << edge_ids.setdefault((key, index), len(edge_ids))
                    memo[key] = ((nodes, edges, height) if nodes.bit_count() <= self.limits.nodes
                                 and edges.bit_count() <= self.limits.edges and height <= self.limits.depth else None)
                    continue
                if key in active:
                    memo[key] = None
                    continue
                if policy is None or not self._direct(request, policy, grants):
                    memo[key] = None
                    continue
                pins = dict(policy.source_versions)
                if (len(pins) != len(policy.source_versions) or set(pins) != set(policy.sources)
                        or len(policy.sources) > self.limits.edges
                        or any(source not in policies or policies[source].version != pins[source]
                               for source in policy.sources)):
                    memo[key] = None
                    continue
                if not policy.sources:
                    memo[key] = True
                    continue
                active.add(key)
                stack.append((key, True))
                stack.extend((source, False) for source in reversed(policy.sources))
        return {key for key, value in memo.items() if value is not None}
