"""Durable access metadata and one administrative service for all frontends.

This module uses the store's transactional SQL port. Authentication remains the
responsibility of the web/host transport; the service accepts only sealed request
contexts from its RequestAuthority, never caller-provided admin flags or UIDs.
"""
from __future__ import annotations

import json
import secrets
import time

from companion_core.runtime_keys import RUN_KEYS

from .access import AccessGrant, AccessService, RecordPolicy, RequestAuthority
from .errors import AssistantError

DDL = """
CREATE TABLE IF NOT EXISTS governance_policies(
 resource TEXT PRIMARY KEY, version TEXT NOT NULL, classification TEXT NOT NULL,
 owners TEXT NOT NULL, conversation TEXT NOT NULL, audience TEXT NOT NULL,
 sources TEXT NOT NULL, valid INTEGER NOT NULL, expires_at REAL, source_versions TEXT NOT NULL,
 CHECK(classification IN ('public','conversation','personal','restricted','secret','unclassified')),
 CHECK(audience IN ('private','group','public')));
CREATE TABLE IF NOT EXISTS governance_grants(
 id TEXT PRIMARY KEY, grantee TEXT NOT NULL, owner TEXT NOT NULL,
 resources TEXT NOT NULL, purposes TEXT NOT NULL, basis TEXT NOT NULL,
 evidence_id TEXT NOT NULL, expires_at REAL NOT NULL, epoch INTEGER NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 CHECK(basis IN ('owner_admin','account_control')));
CREATE INDEX IF NOT EXISTS governance_grants_recipient ON governance_grants(grantee,revoked,expires_at);
CREATE TABLE IF NOT EXISTS governance_audit(
 id TEXT PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
 resource TEXT NOT NULL, before_version TEXT, after_version TEXT);
CREATE TABLE IF NOT EXISTS governance_sessions(
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, purpose TEXT NOT NULL,
 expires_at REAL NOT NULL, epoch INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS governance_challenges(
 id TEXT PRIMARY KEY, left_actor TEXT NOT NULL, right_actor TEXT NOT NULL,
 left_hash TEXT NOT NULL, right_hash TEXT NOT NULL, resources TEXT NOT NULL,
 purposes TEXT NOT NULL, expires_at REAL NOT NULL, epoch INTEGER NOT NULL,
 left_verified INTEGER NOT NULL DEFAULT 0, right_verified INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS governance_provenance(
 resource TEXT PRIMARY KEY REFERENCES governance_policies(resource) ON DELETE CASCADE,
 target TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('scope','snapshot')), origin_version TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS governance_provenance_target ON governance_provenance(target);
"""


class PolicySnapshot:
    """One-check metadata, released promptly without a per-call class/closure cycle."""
    def __init__(self, policies, targets):
        self.policies, self.targets = policies, targets

    def policy(self, resource):
        return self.policies.get(resource)

    def provenance_target(self, resource):
        return self.targets.get(resource)


def resource_id(kind, item_id=None):
    return kind + "/" + (item_id or "@config")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verify_record_versions(store, versions):
    """Recheck exactly the supplied payloads in one query, without reading text."""
    if getattr(store, "backend", None) == "postgres":
        keys = [(kind, item_id or "@config") for kind, item_id in versions]
        with store.db() as db:
            rows = db.execute("SELECT c.kind,c.id,c.version FROM content_records c JOIN "
                              "unnest(?::text[],?::text[]) AS wanted(kind,id) USING(kind,id)",
                              ([key[0] for key in keys], [key[1] for key in keys])).fetchall()
        actual = {(row["kind"], row["id"]): row["version"] for row in rows}
        unchanged = all(actual.get((kind, item_id or "@config")) == version
                        for (kind, item_id), version in versions.items())
    else:
        unchanged = all(store.version(kind, item_id) == version for (kind, item_id), version in versions.items())
    if not unchanged:
        raise AssistantError("context_changed", "已授权资料在处理期间改变")


class GovernanceRepository:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def initialize(self):
        with self.store.db() as db:
            db.executescript(DDL)
        if self.store.state("governance-epoch") is None:
            self.store.set_state("governance-epoch", secrets.randbelow(2**31))

    def epoch(self):
        value = self.store.state("governance-epoch")
        if type(value) is not int:
            raise AssistantError("governance_uninitialized", "资料治理尚未完成初始化")
        return value

    def session_active(self, session_id):
        with self.store.db() as db:
            row = db.execute("SELECT expires_at,epoch,revoked FROM governance_sessions WHERE id=?", (session_id,)).fetchone()
        return bool(row and not row["revoked"] and row["expires_at"] > self.clock() and row["epoch"] == self.epoch())

    def policy(self, resource):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM governance_policies WHERE resource=?", (resource,)).fetchone()
        if not row:
            return None
        return self._policy(row)

    def _policy(self, row):
        parsed = getattr(self.store, "_policy_json", None)
        owners = parsed.sequence(row["owners"]) if parsed else json.loads(row["owners"])
        sources = parsed.sequence(row["sources"]) if parsed else tuple(json.loads(row["sources"]))
        pins = (parsed.sequence(row["source_versions"], pairs=True) if parsed else
                tuple(tuple(item) for item in json.loads(row["source_versions"])))
        return RecordPolicy(row["resource"], row["version"], row["classification"],
                            frozenset(owners), row["conversation"], row["audience"],
                            sources, bool(row["valid"]), row["expires_at"], pins)

    def readable_resources(self, access, request, *, candidates=None):
        """One metadata snapshot avoids a connection/query per ranked record."""
        access.authority.require(request)
        if candidates is not None and getattr(self.store, "backend", None) == "postgres":
            allowed = set()
            candidates = tuple(dict.fromkeys(candidates))
            # Batches bound metadata work; no batch is reused as an authorization cache.
            for offset in range(0, len(candidates), 1000):
                roots = candidates[offset:offset + 1000]
                snapshot = self.disclosure_snapshot(roots, access.limits.nodes)
                reader = AccessService(snapshot, access.authority, max_sources=access.max_sources, limits=access.limits)
                grants = self.grants(request.actor) if request.actor else ()
                allowed.update(set(roots) & reader.readable_snapshot(request, snapshot.policies, grants))
            access.authority.require(request)
            return allowed
        with self.store.db() as db:
            policies = {row["resource"]: self._policy(row) for row in db.execute("SELECT * FROM governance_policies")}
            targets = self.provenance_targets(db, policies)
        grants = self.grants(request.actor) if request.actor else ()

        reader = AccessService(PolicySnapshot(policies, targets), access.authority,
                               max_sources=access.max_sources, limits=access.limits)
        allowed = reader.readable_snapshot(request, policies, grants)
        access.authority.require(request)
        return allowed

    def disclosure_snapshot(self, resources, limit, *, known=None, deadline=None):
        """Bounded metadata read for one check; never reused after the check.

        The dependency index only selects candidates. AccessService still checks
        every actual policy's pins, cycles, scope, grant and the unchanged source
        limit. An incomplete/damaged index therefore denies, never grants access.
        """
        if getattr(self.store, "backend", None) != "postgres":
            return self
        from .provenance import reserved, scope_id

        with self.store.db() as db:
            remaining = max(1, int(((deadline or time.monotonic() + 2) - time.monotonic()) * 1000))
            previous_timeout = db.execute("SHOW statement_timeout").fetchone()[0]
            db.execute("SELECT set_config('statement_timeout', ?, true)", (str(remaining),))
            if known is not None:
                if len(known) > limit:
                    raise AssistantError("access_denied", "来源验证超出节点预算")
                rows = db.execute("SELECT * FROM governance_policies WHERE resource=ANY(?)", (list(known),)).fetchall()
            else:
                rows = db.execute("""WITH RECURSIVE reachable(resource) AS (
                SELECT unnest(?::text[])
                UNION
                SELECT d.source FROM source_dependencies d JOIN reachable r ON d.derived=r.resource
            ) SELECT p.* FROM reachable r JOIN governance_policies p USING(resource) LIMIT ?""",
                                  (list(resources)[:limit + 1], limit + 1)).fetchall()
            policies = {row["resource"]: self._policy(row) for row in rows}
            # Ordinary IDs cannot validate as a registered scope or snapshot.
            # Read every reserved ID afresh, without sending unrelated IDs again.
            virtual_ids = [key for key in policies if reserved(key)]
            virtual = (db.execute("SELECT * FROM governance_provenance WHERE resource=ANY(?)",
                                  (virtual_ids,)).fetchall() if virtual_ids else [])

            # Targets validate reserved IDs; they are not extra readable roots.
            extra = list({key for row in virtual for key in (row["target"], scope_id(row["target"]))} - policies.keys())
            validation = dict(policies)
            if extra:
                validation.update({row["resource"]: self._policy(row) for row in db.execute(
                    "SELECT * FROM governance_policies WHERE resource=ANY(?)", (extra,))})
            targets_to_check = {row["target"] for row in virtual}
            identifiers = [target.split("/", 1)[1] for target in targets_to_check
                           if target.startswith("relationships/")]
            present = ({"relationships/" + row["id"] for row in db.execute(
                "SELECT id FROM content_records WHERE kind='relationships' AND id=ANY(?)", (identifiers,))}
                if identifiers else set())
            if "partition/public-growth" in targets_to_check and self.store.state("public-growth") is not None:
                present.add("partition/public-growth")
            targets = {row["resource"]: row["target"] for row in virtual
                       if row["target"] in present and self._registered_provenance(row, validation)}
            db.execute("SELECT set_config('statement_timeout', ?, true)", (previous_timeout,))

        return PolicySnapshot(policies, targets)

    def grants(self, grantee):
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM governance_grants WHERE grantee=? AND revoked=0 AND expires_at>?",
                              (grantee, self.clock())).fetchall()
        return tuple(AccessGrant(row["id"], row["grantee"], row["owner"], frozenset(json.loads(row["resources"])),
                                 frozenset(json.loads(row["purposes"])), row["basis"], row["evidence_id"],
                                 row["expires_at"], row["epoch"], bool(row["revoked"])) for row in rows)

    def provenance_targets(self, db, policies):
        targets = {}
        exists = {}
        for row in db.execute("SELECT v.* FROM governance_provenance v "
                              "JOIN governance_policies p ON p.resource=v.target"):
            target = row["target"]
            if target not in exists:
                exists[target] = self._aggregate_exists(target)
            if exists[target] and self._registered_provenance(row, policies):
                targets[row["resource"]] = target
        return targets

    def _aggregate_exists(self, target):
        from .provenance import mutable

        if not mutable(target):
            return False
        return (self.store.state("public-growth") is not None if target == "partition/public-growth"
                else self.store.exists("relationships", target.split("/", 1)[1]))

    def _registered_provenance(self, row, policies=None):
        from hashlib import sha256

        from .provenance import PREFIX, permissions, scope_id

        lookup = self.policy if policies is None else policies.get
        policy = lookup(row["resource"])
        scope = lookup(scope_id(row["target"]))
        target = lookup(row["target"])
        if not policy or not scope or not target or permissions(scope) != permissions(target):
            return False
        if row["kind"] == "scope":
            return (row["resource"] == scope_id(row["target"]) and row["origin_version"] == ""
                    and not policy.sources and not policy.source_versions)
        if (policy.version != row["origin_version"] or permissions(policy) != permissions(scope)
                or scope.resource not in policy.sources
                or set(policy.sources) != {key for key, _ in policy.source_versions}):
            return False
        scope_version = dict(policy.source_versions).get(scope_id(row["target"]))
        if not scope_version:
            return False
        expected = PREFIX + "snapshot/" + sha256(
            (row["target"] + "\n" + row["origin_version"] + "\n" + scope_version).encode()).hexdigest()
        return row["resource"] == expected

    def provenance_target(self, resource):
        with self.store.db() as db:
            row = db.execute("SELECT v.* FROM governance_provenance v "
                             "JOIN governance_policies p ON p.resource=v.target WHERE v.resource=?",
                             (resource,)).fetchone()
        if row and self._aggregate_exists(row["target"]) and self._registered_provenance(row):
            return row["target"]
        return None

    def set_policy(self, policy, expected, actor, *, db=None, _provenance=None):
        from .provenance import _WRITE, reserved, sync_scope

        if reserved(policy.resource) and _provenance is not _WRITE:
            raise AssistantError("provenance_reserved", "历史来源只能由核心冻结，不能编辑或单独授权")
        if db is None:
            with self.store.lock, self.store.db() as connection:
                return self.set_policy(policy, expected, actor, db=connection, _provenance=_provenance)
        if getattr(db, "dialect", "sqlite") == "postgres":
            db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM governance_policies WHERE resource=?", (policy.resource,)).fetchone()
        actual = current["version"] if current else None
        if actual != expected:
            raise AssistantError("edit_conflict", "资料权限版本已变化")
        values = (policy.resource, policy.version, policy.classification, _json(sorted(policy.owners)),
                  policy.conversation, policy.audience, _json(policy.sources), int(policy.valid), policy.expires_at,
                  _json(policy.source_versions))
        if actual is None:
            cursor = db.execute("INSERT INTO governance_policies VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", values)
        else:
            cursor = db.execute("""UPDATE governance_policies SET version=?,classification=?,owners=?,conversation=?,
                               audience=?,sources=?,valid=?,expires_at=?,source_versions=? WHERE resource=? AND version=?""",
                               (*values[1:], policy.resource, expected))
        if cursor.rowcount != 1:
            raise AssistantError("edit_conflict", "资料权限已由另一入口修改")
        if getattr(db, "dialect", "sqlite") == "postgres":
            db.execute("DELETE FROM record_owners WHERE resource=?", (policy.resource,))
            db.executemany("INSERT INTO record_owners VALUES(?,?)", [(policy.resource, owner) for owner in policy.owners])
            db.execute("DELETE FROM source_dependencies WHERE derived=?", (policy.resource,))
            db.executemany("INSERT INTO source_dependencies VALUES(?,?,?)",
                           [(policy.resource, source, version) for source, version in policy.source_versions])
        self.audit(actor, "policy", policy.resource, actual, policy.version, db=db)
        sync_scope(self, self._policy(current) if current else None, policy, db)

    def audit(self, actor, operation, resource, before=None, after=None, *, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.audit(actor, operation, resource, before, after, db=connection)
        db.execute("INSERT INTO governance_audit VALUES(?,?,?,?,?,?,?)",
                   (secrets.token_hex(16), self.clock(), actor, operation, resource, before, after))

    def put_grant(self, grant, actor, *, db=None):
        from .provenance import reserved

        if any(reserved(resource) for resource in grant.resources):
            raise AssistantError("grant_scope", "历史来源不能单独授予读取权限")
        if db is None:
            with self.store.db() as connection:
                return self.put_grant(grant, actor, db=connection)
        db.execute("INSERT INTO governance_grants VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (grant.id, grant.grantee, grant.owner, _json(sorted(grant.resources)), _json(sorted(grant.purposes)),
                    grant.basis, grant.evidence_id, grant.expires_at, grant.epoch, int(grant.revoked), self.clock()))
        if getattr(db, "dialect", "sqlite") == "postgres":
            db.executemany("INSERT INTO grant_resources VALUES(?,?)", [(grant.id, resource) for resource in grant.resources])
        self.audit(actor, "grant", grant.id, db=db)

    def reset_after_restore(self):
        with self.store.lock, self.store.db() as db:
            self.store.set_state("governance-epoch", secrets.randbits(62), db)
            db.execute("UPDATE governance_sessions SET revoked=1")
            db.execute("UPDATE governance_grants SET revoked=1")
            db.execute("UPDATE governance_challenges SET consumed=1")
            self.store.set_state("service-backup", {}, db)
            for key in RUN_KEYS:
                run = self.store.state(key, {}, db)
                if run:
                    self.store.set_state(key, {**run, "status": "paused", "reason": "restore"}, db)
            self.audit("restore", "revoke_all_access", "database", db=db)


class AdminService:
    def __init__(self, store, repository=None, authority=None):
        self.store = store
        self.repository = repository or GovernanceRepository(store)
        self.authority = authority or RequestAuthority(epoch=self.repository.epoch,
                                                       session_active=self.repository.session_active,
                                                       clock=self.repository.clock)
        self.access = AccessService(self.repository, self.authority)

    def read_config(self, request, kind):
        self.authority.require(request, administrator=True)
        if kind not in self.store.profile.config:
            raise AssistantError("invalid_config", "未知配置类别")
        return self.store.read(kind)

    def patch_config(self, request, kind, changes, expected):
        self.authority.require(request, administrator=True)
        if kind not in self.store.profile.config or not isinstance(changes, dict):
            raise AssistantError("invalid_config", "配置修改格式无效")
        from . import baseline

        with self.store.lock, self.store.db() as db, baseline.manual_edit(self.store):
            if getattr(db, "dialect", "sqlite") == "postgres":
                db.execute("BEGIN IMMEDIATE")
            record = self.store.read(kind)
            if record["version"] != expected:
                raise AssistantError("edit_conflict", "配置已由另一入口修改，请重新加载")
            # Exactly the same schema, version check and audit for chat and web.
            result = self.store.write(kind, {**record["data"], **changes}, expected,
                                      "admin:" + request.actor)
            if kind == "persona":
                baseline.sync_manual_card(self.store, record["data"], request.actor)
            self.repository.audit(request.actor, "config", resource_id(kind), expected, result["version"])
            return result

    def classify(self, request, *, resource, expected, classification, owners=(),
                 conversation="", audience="private", sources=(), valid=True):
        self.authority.require(request, administrator=True)
        # Changing visibility is an explicit admin operation, never a proposed
        # model write. Fresh revision invalidates every previously issued lease.
        pinned = []
        for source in sources:
            item = self.repository.policy(source)
            if item is None:
                raise AssistantError("source_missing", "来源尚未登记权限")
            pinned.append((source, item.version))
        policy = RecordPolicy(resource, secrets.token_hex(16), classification, frozenset(owners),
                              conversation, audience, tuple(sources), valid, source_versions=tuple(pinned))
        self.repository.set_policy(policy, expected, request.actor)
        return {"resource": resource, "version": policy.version, "classification": classification}

    def authorize(self, request, *, grantee, owner, resources, purposes=("reply",), lifetime=86400 * 30):
        self.authority.require(request, administrator=True)
        if (not grantee or not owner or not resources or len(resources) > 100
                or not purposes or not set(purposes) <= {"reply", "maintenance"}
                or not 0 < lifetime <= 86400 * 365):
            raise AssistantError("grant_scope", "授权必须明确账号、资料、用途和有效期")
        for resource in resources:
            from .provenance import reserved

            policy = self.repository.policy(resource)
            if (reserved(resource) or not policy or owner not in policy.owners
                    or policy.classification in ("secret", "unclassified")):
                raise AssistantError("grant_scope", "授权包含归属或分类未确认的资料")
        grant = AccessGrant(secrets.token_hex(16), grantee, owner, frozenset(resources), frozenset(purposes),
                            "owner_admin", request.session_id or "trusted-admin", self.repository.clock() + lifetime,
                            self.repository.epoch())
        self.repository.put_grant(grant, request.actor)
        return {"id": grant.id, "expires_at": grant.expires_at, "resources": sorted(grant.resources)}

    def revoke(self, request, grant_id):
        self.authority.require(request, administrator=True)
        with self.store.db() as db:
            db.execute("UPDATE governance_grants SET revoked=1 WHERE id=?", (grant_id,))
            self.repository.audit(request.actor, "revoke", grant_id, db=db)
        return {"revoked": True}


class ContextService:
    """Retrieve only authorized material; freeze all policy and source revisions."""
    def __init__(self, store, access):
        self.store, self.access = store, access

    def memory(self, request, query, *, references=(), uids=(), queries=()):
        readable = self.access.repository.readable_resources(self.access, request)

        def allowed(item):
            return resource_id(item["kind"], item["data"]["id"]) in readable

        view = ReadView(self.store, readable)
        context = view.context(query, references, uids, queries, record_filter=allowed)
        used = {resource_id(item["kind"], item["data"]["id"]) for item in context["records"]}
        used.update(resource_id(item["kind"], item["id"]) for item in context["catalog"])
        lease = self.access.lease(request, used)
        # Diagnostics and annotations can reveal hidden owner IDs, foundation IDs
        # or cross-source review text. Return only records with separately verified
        # sources; callers can request those sources through this same service.
        context["records"] = [{key: value for key, value in item.items()
                                if key in {"kind", "data", "version", "content_truncated", "matched_excerpt"}}
                               for item in context["records"]]
        context.pop("relationship_scope", None)
        context.pop("foundation_status", None)
        context["total_context_chars"] = len(_json(context))
        versions = {(item["kind"], item["data"]["id"]): item["version"] for item in context["records"]}
        versions.update({(item["kind"], item["id"]): item["version"] for item in context["catalog"]})

        def revalidate():
            self.access.revalidate(lease)
            verify_record_versions(self.store, versions)

        revalidate()
        return context, revalidate

    def optional(self, request, kind, item_id=None, default=None):
        if not self.access.can_read(request, resource_id(kind, item_id)):
            return default
        return self.get(request, kind, item_id)

    def get(self, request, kind, item_id):
        lease = self.access.lease(request, [resource_id(kind, item_id)])
        result = self.store.read(kind, item_id)
        self.access.revalidate(lease)
        return result


def enabled(store):
    return getattr(store, "backend", None) == "postgres" or bool(store.state("governance-enabled", False))


class ReadView:
    """Reuse retrieval algorithms on an already-authorized corpus.

    No plaintext index is written for PostgreSQL. Configs used internally to
    bound retrieval are not themselves returned as role material.
    """
    def __init__(self, store, readable):
        self.store, self.readable = store, readable

    def __getattr__(self, name):
        return getattr(self.store, name)

    def records(self, kind):
        ids = [resource.split("/", 1)[1] for resource in self.readable if resource.startswith(kind + "/")]
        if hasattr(self.store, "records_selected"):
            return self.store.records_selected(kind, ids)
        return [r for r in self.store.records(kind)[0] if r["data"]["id"] in set(ids)], []

    def read(self, kind, item_id=None):
        if kind == "manifest" and item_id is None:
            item = self.store.read(kind)
            return {"data": {"account_uid": item["data"]["account_uid"]}, "version": item["version"]}
        if kind in ("companion", "persona") and item_id is None:
            original = self.store.read(kind)
            if kind == "persona" and resource_id(kind) not in self.readable:
                return {"data": {}, "version": original["version"]}
            return original
        if resource_id(kind, item_id) not in self.readable:
            raise AssistantError("not_found", "资料不存在或无权访问")
        return self.store.read(kind, item_id)

    def context(self, *args, **kwargs):
        from .storage import Store

        return Store.context(self, *args, **kwargs)

    def search(self, query, limit=10, *, queries=(), uids=()):
        from .storage import Store

        return Store.search(self, query, limit, queries=queries, uids=uids)


def register_event(store, event, version, *, db=None):
    repository = GovernanceRepository(store)
    resource = "event/" + event.id
    current = repository.policy(resource)
    classification = {"private": "personal", "public": "public", "group": "conversation"}[event.visibility]
    policy = RecordPolicy(resource, version, classification, frozenset((event.actor.key,)),
                          event.thread, event.visibility, valid=current.valid if current else True)
    repository.set_policy(policy, current.version if current else None, "trusted-ingress", db=db)


def record_provenance(store, kind, item_id, proof):
    """Commit a previously frozen disclosure scope to a new derived record."""
    repository = GovernanceRepository(store)
    resource = resource_id(kind, item_id)
    current = repository.policy(resource)
    pins = tuple((source, version) for source, version in proof["pins"] if source != resource)
    classification = "public" if proof["audience"] == "public" else "conversation"
    policy = RecordPolicy(resource, secrets.token_hex(16), classification, frozenset(proof["owners"]),
                          proof["conversation"], proof["audience"], tuple(source for source, _ in pins),
                          source_versions=pins)
    repository.set_policy(policy, current.version if current else None, "derived-provenance")


class GovernedContext:
    """One recipient's authorized sources, from retrieval through final dispatch.

    Internal routing settings are not disclosed. Unknown legacy material stays
    quarantined; a missing approved persona produces a small neutral fallback.
    """
    def __init__(self, store, event, authority=None):
        self.store, self.event = store, event
        self.repository = GovernanceRepository(store)
        self.authority = authority or RequestAuthority(epoch=self.repository.epoch,
                                                       session_active=self.repository.session_active)
        self.request = self.authority.for_event(event)
        self.access = AccessService(self.repository, self.authority)
        self.contexts = ContextService(store, self.access)
        self.resources, self.versions, self.guards = set(), {}, []

    def read(self, kind, item_id=None, default=None):
        resource = resource_id(kind, item_id)
        if not self.access.can_read(self.request, resource):
            return default
        item = self.contexts.get(self.request, kind, item_id)
        self.resources.add(resource)
        self.versions[kind, item_id] = item["version"]
        return item

    def records(self, kind):
        readable = self.repository.readable_resources(self.access, self.request)
        return ReadView(self.store, readable).records(kind)

    def memory(self, query, *, references=(), uids=(), queries=()):
        result, guard = self.contexts.memory(self.request, query, references=references, uids=uids, queries=queries)
        guard()
        self.resources.update(resource_id(item["kind"], item["data"]["id"]) for item in result["records"])
        self.resources.update(resource_id(item["kind"], item["id"]) for item in result["catalog"])
        self.versions.update({(item["kind"], item["data"]["id"]): item["version"] for item in result["records"]})
        self.versions.update({(item["kind"], item["id"]): item["version"] for item in result["catalog"]})
        return result

    def conversation(self, account, tid, *, query="", queries=()):
        from .conversations import context

        owner = self

        class View:
            def records(self, kind):
                if getattr(owner.store, "backend", None) == "postgres" and kind in (
                        "conversation_turns", "conversation_summaries"):
                    from .conversation_index import candidates

                    roots = [resource_id(kind, value) for value in candidates(owner.store, kind, account, tid)]
                    allowed = owner.repository.readable_resources(owner.access, owner.request, candidates=roots)
                    return ReadView(owner.store, allowed).records(kind)
                return owner.records(kind)

            def exists(self, kind, item_id=None):
                return owner.access.can_read(owner.request, resource_id(kind, item_id)) and owner.store.exists(kind, item_id)

            def read(self, kind, item_id=None):
                # Only numeric budgeting parameters are used from this config.
                if kind == "companion":
                    return owner.store.read(kind)
                value = owner.read(kind, item_id)
                if value is None:
                    raise AssistantError("access_denied", "对话资料不可在当前会话使用")
                return value

        result = context(View(), account, tid, query=query, queries=queries)
        for item in [*result["turns"], *result["recalled"]]:
            self.resources.add(resource_id("conversation_turns", item["data"]["id"]))
            self.versions["conversation_turns", item["data"]["id"]] = item["version"]
        if result["summary"]:
            self.resources.add(resource_id("conversation_summaries", result["summary"]["id"]))
            self.versions["conversation_summaries", result["summary"]["id"]] = result["summary"]["version"]
        return result

    def conversation_signature(self, account, tid):
        """Detect membership/content changes without decrypting the archive again.

        This is NOT an ACL cache. check() still revalidates the complete source
        closure for every item actually supplied to the model.
        """
        import hashlib

        with self.store.db() as db:
            rows = db.execute("SELECT i.kind,i.id,c.version,p.version FROM conversation_index i "
                "JOIN content_records c USING(kind,id) LEFT JOIN governance_policies p ON p.resource=i.kind || '/' || i.id "
                "WHERE i.account=? AND i.thread_id=? ORDER BY i.kind,i.id", (account, tid)).fetchall()
        return hashlib.sha256(_json([list(row) for row in rows]).encode()).hexdigest()

    def freeze(self):
        from .provenance import freeze

        self.lease, self.frozen_sources = freeze(self.access, self.access.lease(self.request, self.resources))
        return self

    def proof(self):
        return {"owners": [self.request.actor], "conversation": self.request.conversation,
                "audience": self.request.audience, "pins": list(self.frozen_sources)}

    def rebase_written(self, kind, item_id, version):
        from .provenance import rebase

        resource = resource_id(kind, item_id)
        if (kind, item_id) in self.versions:
            self.versions[kind, item_id] = version
        policy = self.repository.policy(resource)
        self.lease = rebase(self.access, self.lease, resource, policy.version, self.frozen_sources)
        self.check()

    def check(self):
        self.access.revalidate(self.lease)
        for guard in self.guards:
            guard()
        verify_record_versions(self.store, self.versions)

    def classify_written(self, kind, item_id, *, personal=False, sources=None):
        """Trusted commit hook; model output cannot select its own permissions."""
        resource = resource_id(kind, item_id)
        previous = self.repository.policy(resource)
        if sources is None:
            pins = [(key, version) for key, version in self.frozen_sources if key != resource]
        else:
            pins = []
            for source in sorted(set(sources) - {resource}):
                policy = self.repository.policy(source)
                if policy is None:
                    raise AssistantError("source_missing", "派生资料缺少权限来源")
                pins.append((source, policy.version))
        dependencies = tuple(key for key, _ in pins)
        classification = ("public" if self.request.audience == "public" else
                          "personal" if personal and self.request.audience == "private" else "conversation")
        policy = RecordPolicy(resource, secrets.token_hex(16), classification,
                              frozenset((self.request.actor,)), self.request.conversation,
                              self.request.audience, dependencies, source_versions=tuple(pins))
        self.repository.set_policy(policy, previous.version if previous else None, "core-provenance")

    def classify_event(self):
        # A verified inbound event is owned by its actual sender. Mentioning
        # another user does not make that other user an owner of this assertion.
        resource = "event/" + self.event.id
        current = self.repository.policy(resource)
        classification = "personal" if self.event.visibility == "private" else "conversation"
        policy = RecordPolicy(resource, secrets.token_hex(16), classification,
                              frozenset((self.event.actor.key,)), self.event.thread, self.event.visibility)
        if current is None:
            self.repository.set_policy(policy, None, "trusted-ingress")
        self.resources.add(resource)

    def persona_context(self):
        from . import baseline

        bundle = baseline.load(self.store, read=self.read)
        if bundle:
            self.guards.append(bundle.check)
            return bundle.context
        return {"persona": self.persona()}

    def persona(self):
        item = self.read("persona")
        if item:
            return item["data"]
        return {"name": "小龙娘", "description": "以自然、友善的方式讨论本次提供的内容。"}
