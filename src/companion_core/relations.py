"""Source-bearing user-to-user claims and bounded, per-edge authorized traversal."""
from __future__ import annotations

import secrets
import time
from dataclasses import replace

from .access import AccessService, RecordPolicy
from .errors import AssistantError


class RelationService:
    def __init__(self, repository, authority):
        self.repository, self.authority = repository, authority
        self.store = repository.store
        self.access = AccessService(repository, authority)

    def register_actor(self, actor):
        # Called by trusted ingress/admin, never as a model capability. The
        # public shell reveals no username/UID; the identifying body is private.
        with self.store.db() as db:
            db.execute("INSERT INTO actor_nodes VALUES(?,?,?) ON CONFLICT DO NOTHING",
                       (actor.key, actor.platform, self.store.ledger_encode("actor_nodes.identity", actor.as_dict())))
            resource = "actor/" + actor.key
            old = self.repository.policy(resource)
            if old is None:
                self.repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "public"), None, "actor-ingress", db=db)

    def propose(self, request, *, subject, object, relation, evidence, reason=""):
        self.authority.require(request)
        if (not request.actor or subject == object or not 1 <= len(relation) <= 80 or len(reason) > 1000
                or not 1 <= len(evidence) <= 16):
            raise AssistantError("relation_scope", "关系声明需要不同主体、有限描述和实际来源")
        lease = self.access.lease(request, evidence)
        for source in evidence:
            policy = self.repository.policy(source)
            if request.actor not in policy.owners:
                raise AssistantError("relation_scope", "关系依据必须由当前声明者提交或确认")
        identifier, version = secrets.token_hex(16), secrets.token_hex(16)
        body = {"relation": relation, "reason": reason, "evidence": list(evidence), "direction": "subject_to_object"}
        with self.store.lock, self.store.db() as db:
            self.access.revalidate(lease)
            for actor in (subject, object):
                if not db.execute("SELECT 1 FROM actor_nodes WHERE actor_key=?", (actor,)).fetchone():
                    raise AssistantError("relation_scope", "关系节点尚未由真实账号或管理入口登记")
            db.execute("INSERT INTO relationship_claims VALUES(?,?,?,?,?,?,?)",
                       (identifier, subject, object, request.actor, "claimed", version,
                        self.store.ledger_encode("relationship_claims.body", body)))
            resource = "relation/" + identifier
            policy = RecordPolicy(resource, version, "personal" if request.audience == "private" else "conversation",
                                  frozenset((request.actor,)), request.conversation, request.audience,
                                  tuple(evidence), source_versions=tuple((source, self.repository.policy(source).version)
                                                                       for source in evidence))
            self.repository.set_policy(policy, None, request.actor, db=db)
        return {"id": identifier, "version": version, "status": "claimed"}

    def adjudicate(self, request, identifier, status, expected):
        self.authority.require(request, administrator=True)
        if status not in ("confirmed", "disputed", "revoked"):
            raise AssistantError("relation_status", "关系状态无效")
        with self.store.lock, self.store.db() as db:
            version = secrets.token_hex(16)
            result = db.execute("UPDATE relationship_claims SET status=?,version=? WHERE id=? AND version=?",
                                (status, version, identifier, expected))
            if result.rowcount != 1:
                raise AssistantError("edit_conflict", "关系声明已变化")
            policy = self.repository.policy("relation/" + identifier)
            if policy is None:
                raise AssistantError("relation_scope", "关系缺少权限来源")
            self.repository.set_policy(replace(policy, version=version, valid=status != "revoked"),
                                       policy.version, request.actor, db=db)
        return {"id": identifier, "version": version, "status": status}

    def query(self, request, actor_key, *, depth=1, max_nodes=20, max_edges=40, max_chars=6000):
        self.authority.require(request)
        if (type(depth) is not int or not 1 <= depth <= 2 or not 1 <= max_nodes <= 30
                or not 1 <= max_edges <= 60 or not 500 <= max_chars <= 10000):
            raise AssistantError("relation_limit", "关系查询超过层数或数量限制")
        readable = self.repository.readable_resources(self.access, request)
        if "actor/" + actor_key not in readable:
            return {"claims": [], "depth": depth, "truncated": False}
        allowed = [key.split("/", 1)[1] for key in readable if key.startswith("relation/")]
        nodes = [key.split("/", 1)[1] for key in readable if key.startswith("actor/")]
        frontier, visited, used, claims = {actor_key}, {actor_key}, set(), []
        chars, started, truncated = 0, time.monotonic(), False
        for distance in range(depth):
            next_frontier = set()
            with self.store.db() as db:
                # Hidden endpoints and already visited edges must not consume the
                # visible page. Otherwise a hidden prefix masks later valid paths
                # and reveals its presence through result counts/truncation.
                rows = db.execute("""WITH allowed_claims AS (SELECT unnest(?::text[]) AS id),
                                  allowed_nodes AS (SELECT unnest(?::text[]) AS actor_key)
                                  SELECT c.* FROM relationship_claims c
                                  JOIN allowed_claims a ON a.id=c.id
                                  JOIN allowed_nodes s ON s.actor_key=c.subject
                                  JOIN allowed_nodes o ON o.actor_key=c.object
                                  WHERE c.status!='revoked' AND c.id!=ALL(?)
                                  AND (c.subject=ANY(?) OR c.object=ANY(?)) ORDER BY c.id LIMIT ?""",
                                  (allowed, nodes, list(used), list(frontier), list(frontier),
                                   max_edges - len(claims) + 1)).fetchall()
            for row in rows:
                if row["id"] in used:
                    continue
                endpoints = {row["subject"], row["object"]}
                if any("actor/" + node not in readable for node in endpoints):
                    continue
                body = self.store.ledger_decode("relationship_claims.body", row["body"])
                length = len(body["relation"]) + len(body["reason"]) + 250
                if (len(claims) >= max_edges or len(visited | endpoints) > max_nodes
                        or chars + length > max_chars or time.monotonic() - started > 1):
                    truncated = True
                    break
                claims.append({"id": row["id"], "subject": row["subject"], "object": row["object"],
                               "declarer": row["declarer"], "status": row["status"], "hop": distance + 1, **body})
                used.add(row["id"])
                next_frontier |= endpoints - visited
                visited |= endpoints
                chars += length
            if truncated or not next_frontier:
                break
            frontier = next_frontier
        self.access.lease(request, [*["relation/" + item["id"] for item in claims],
                                    *["actor/" + node for node in visited]])
        return {"claims": claims, "depth": depth, "truncated": truncated,
                "inferred_relationships": False, "access_transitive": False}
