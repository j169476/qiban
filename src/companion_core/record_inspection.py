"""Administrator-only source diagnostics and bounded deletion impact, no LLM needed."""
import hashlib
import json

from .errors import AssistantError
from .governance import GovernanceRepository


def impact(store, resource):
    with store.db() as db:
        previous = db.execute("SHOW statement_timeout").fetchone()[0]
        db.execute("SELECT set_config('statement_timeout','3000',true)")
        rows = db.execute("""WITH RECURSIVE affected(resource) AS (
            SELECT ?::text UNION SELECT d.derived FROM source_dependencies d JOIN affected a ON d.source=a.resource
          ), virtual(resource) AS (SELECT resource FROM governance_provenance WHERE target=?),
          all_affected(resource) AS (
            SELECT resource FROM affected UNION SELECT resource FROM virtual
            UNION SELECT d.derived FROM source_dependencies d JOIN all_affected a ON d.source=a.resource
          ) SELECT a.resource,p.version,p.valid FROM all_affected a LEFT JOIN governance_policies p USING(resource)
            ORDER BY a.resource LIMIT 10001""", (resource, resource)).fetchall()
        db.execute("SELECT set_config('statement_timeout',?,true)", (previous,))
    if len(rows) > 10000:
        raise AssistantError("impact_limit", "影响范围超过网页预览上限，请先分批整理资料")
    entries = [dict(row) for row in rows]
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    return {"fingerprint": digest, "affected_count": len(entries),
            "sample": [row["resource"] for row in entries[:50]], "sample_limited": len(entries) > 50}


def explain(store, resource):
    repository = GovernanceRepository(store)
    snapshot = repository.disclosure_snapshot([resource], 10000)
    policies = snapshot.policies
    nodes, issues = [], []
    for key, policy in sorted(policies.items()):
        if not policy.valid:
            issues.append({"resource": key, "reason": "revoked"})
        if policy.classification in {"unclassified", "secret"}:
            issues.append({"resource": key, "reason": policy.classification})
        if policy.expires_at is not None and policy.expires_at <= repository.clock():
            issues.append({"resource": key, "reason": "expired"})
        pins = dict(policy.source_versions)
        if set(pins) != set(policy.sources):
            issues.append({"resource": key, "reason": "incomplete_source_versions"})
        for source in policy.sources:
            current = policies.get(source)
            if current is None or current.version != pins.get(source):
                issues.append({"resource": key, "reason": "source_missing_or_changed", "source": source})
        if len(nodes) < 200:
            nodes.append({"resource": key, "version": policy.version, "classification": policy.classification,
                          "owners": sorted(policy.owners), "conversation": policy.conversation,
                          "audience": policy.audience, "valid": policy.valid, "source_count": len(policy.sources),
                          "sources": list(policy.sources[:30])})
    if resource not in policies:
        raise AssistantError("not_found", "资料权限记录不存在")
    return {"root": resource, "nodes": nodes, "total_nodes": len(policies), "limited": len(policies) > 200,
            "issues": issues[:200], "total_issues": len(issues), "impact": impact(store, resource),
            "meaning": "这些是程序记录的来源与权限依据。可读取仍由实际接收账号、会话、有效授权及全部来源版本共同决定；不推断模型内心原因。"}
