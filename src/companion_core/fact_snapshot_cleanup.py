"""Collect only unusable, unreferenced automatic fact provenance nodes."""
import re
import time

from .governance import GovernanceRepository
from .source_retention import held

PREFIX = 'personal-fact-view/'
AUTOMATIC = re.compile(r'personal-fact-view/fact-view-(?:part-)?[a-f0-9]{40}\Z')


def stale(repository, resource, now, *, deadline):
    snapshot = repository.disclosure_snapshot([resource], 10000, deadline=deadline)
    if len(snapshot.policies) > 10000:
        return None
    pending, seen = [resource], set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        policy = snapshot.policy(key)
        if policy is None:
            # A damaged dependency index is not proof that the source is gone.
            return True if repository.policy(key) is None else None
        if not policy.valid or policy.expires_at is not None and policy.expires_at <= now:
            return True
        pins = dict(policy.source_versions)
        if set(pins) != set(policy.sources):
            return None
        for source in policy.sources:
            child = snapshot.policy(source)
            if child is None:
                return True if repository.policy(source) is None else None
            if child.version != pins[source]:
                return True
            pending.append(source)
    return False


def collect(store, db, *, guard, now, limit=50):
    repository = GovernanceRepository(store)
    cursor = store.state('fact-view-cleanup-cursor', '')
    rows = db.execute('SELECT resource FROM governance_policies WHERE resource LIKE ? AND resource>? '
                      'ORDER BY resource LIMIT ?', (PREFIX + '%', cursor, limit)).fetchall()
    removed = unverified = 0
    examined = []
    deadline = time.monotonic() + 2
    for row in rows:
        if time.monotonic() >= deadline:
            break
        resource = row['resource']
        examined.append(resource)
        if not AUTOMATIC.fullmatch(resource) or held(db, resource):
            continue
        if db.execute('SELECT 1 FROM source_dependencies WHERE source=? LIMIT 1', (resource,)).fetchone():
            continue
        if db.execute('SELECT 1 FROM grant_resources WHERE resource=? LIMIT 1', (resource,)).fetchone():
            continue
        if db.execute('SELECT 1 FROM governance_challenges WHERE jsonb_exists(resources::jsonb,?) LIMIT 1',
                      (resource,)).fetchone():
            continue
        if not db.execute("SELECT 1 FROM governance_audit WHERE resource=? AND actor='personal-fact-projection' "
                          "AND before_version IS NULL LIMIT 1", (resource,)).fetchone():
            continue
        invalid = stale(repository, resource, now, deadline=deadline)
        if invalid is None:
            unverified += 1
        if not invalid:
            continue
        guard()
        db.execute('DELETE FROM governance_policies WHERE resource=?', (resource,))
        repository.audit('fact-snapshot-cleanup', 'collect_unused_snapshot', resource, db=db)
        removed += 1
    continuing = len(examined) < len(rows) or len(rows) == limit
    # Same rule as `fact_retirement.cleanup`: only a cursor that moved is a change, because
    # `set_state` re-encrypts with a fresh nonce and the portable preview compares a store
    # fingerprint between its preview and its switch.
    position = (examined[-1] if examined else cursor) if continuing else ''
    if store.state('fact-view-cleanup-cursor', '') != position:
        store.set_state('fact-view-cleanup-cursor', position, db)
    return {'examined': len(examined), 'removed': removed, 'retained': len(examined) - removed,
            'unverified': unverified, 'status': 'checked'}
