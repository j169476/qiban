"""Source-bearing user-to-user claims and bounded, per-edge authorized traversal."""
from __future__ import annotations

import secrets
import time
from dataclasses import replace

from . import relation_history
from .access import AccessService, RecordPolicy
from .errors import AssistantError

STATUSES = ("claimed", "confirmed", "disputed", "revoked")
# Only the core semantic: a revoked claim may be confirmed or disputed again.
TARGETS = ("confirmed", "disputed", "revoked")
ANOMALY_DETAIL = {
    "policy_missing": "这条声明的权限记录缺失。",
    "status_valid_mismatch": "这条声明的状态与它的权限有效标记不一致。",
}


def inconsistency(status, policy):
    """Report a stored claim/policy disagreement, never silently repair it.

    Adjudication derives ``valid`` from the requested status, so an already
    inconsistent pair would silently turn a policy that was invalidated for
    another reason back into a valid one (and make a source readable again).
    Callers must stop and report this instead.
    """
    if policy is None:
        return "policy_missing"
    if bool(policy.valid) != (status != "revoked"):
        return "status_valid_mismatch"
    return None


def require_consistent(status, policy):
    anomaly = inconsistency(status, policy)
    if anomaly is not None:
        raise AssistantError("relation_inconsistent", ANOMALY_DETAIL[anomaly]
                             + "本页不会默默把它改回有效；请先由维护流程核对这条声明与它的来源。")
    return anomaly


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

    def adjudicate(self, request, identifier, status, expected, reason=""):
        """Change only the status of one existing claim, keeping its history.

        ``reason`` is optional and stays inside the history entry; the claim body
        (statement, subject, object, declarer, evidence) is never rewritten, the
        sources are never re-pinned and every other permission keeps its stored
        value. A same-status request is refused rather than given a new version.

        The history entry is appended in this same transaction, so the old web
        entry point keeps the same record as the desktop page.
        """
        self.authority.require(request, administrator=True)
        if status not in TARGETS:
            raise AssistantError("relation_status", "关系状态无效")
        if not isinstance(reason, str) or len(reason) > relation_history.REASON_MAX:
            raise AssistantError("relation_reason", f"更正说明最多 {relation_history.REASON_MAX} 字。")
        with self.store.lock, self.store.db() as db:
            row = db.execute("SELECT subject,object,declarer,status,version,body "
                             "FROM relationship_claims WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise AssistantError("relation_scope", "关系声明不存在")
            if row["version"] != expected:
                raise AssistantError("edit_conflict", "关系声明已变化")
            if row["status"] == status:
                raise AssistantError("relation_status", "关系状态没有变化，未增加新版本")
            policy = self.repository.policy("relation/" + identifier)
            require_consistent(row["status"], policy)
            version = secrets.token_hex(16)
            result = db.execute("UPDATE relationship_claims SET status=?,version=? WHERE id=? AND version=?",
                                (status, version, identifier, expected))
            if result.rowcount != 1:
                raise AssistantError("edit_conflict", "关系声明已变化")
            updated = replace(policy, version=version, valid=status != "revoked")
            self.repository.set_policy(updated, policy.version, request.actor, db=db)
            history = relation_history.append(
                self.store, db=db, claim=identifier,
                before={"status": row["status"], "version": row["version"],
                        "policy_version": policy.version, "valid": bool(policy.valid)},
                after={"status": status, "version": version,
                       "policy_version": updated.version, "valid": bool(updated.valid)},
                actor=request.actor, at=self.repository.clock(), reason=reason,
                snapshot=self._snapshot(row), sources=policy.source_versions)
        return {"id": identifier, "version": version, "status": status, "history": history,
                "restored": history["restored"]}

    def _snapshot(self, row):
        try:
            body = self.store.ledger_decode("relationship_claims.body", row["body"])
        except AssistantError:
            # A damaged body is recorded as unreadable; it is never reconstructed.
            body = {"unreadable": True}
        return {"subject": row["subject"], "object": row["object"], "declarer": row["declarer"],
                "status": row["status"], "version": row["version"], "body": body}

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
