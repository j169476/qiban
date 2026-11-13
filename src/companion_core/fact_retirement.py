"""Explicitly retired facts only: no age-based eviction of useful history."""
import hashlib
import json
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .errors import AssistantError
from .fact_index import scope
from .governance import GovernanceRepository

PREFIX = 'fact-retired:'
GUARD = 'fact-retired-scope:'
SETTINGS = 'fact-history-retention'


def settings(store):
    return store.state(SETTINGS, {'days': 90, 'revision': 'initial'})


def configure(store, days, expected, actor):
    if type(days) is not int or not 1 <= days <= 3650:
        raise AssistantError('invalid_query', '已停用事实的历史保留期为 1 至 3650 天')
    with store.lock, store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if settings(store)['revision'] != expected:
            raise AssistantError('edit_conflict', '历史保留设置已改变，请重新读取')
        value = {'days': days, 'revision': secrets.token_hex(16)}
        store.set_state(SETTINGS, value, db)
        GovernanceRepository(store).audit(actor, 'fact_retention_changed', SETTINGS, db=db)
        return value


def identity(store, fact):
    return scope(store, fact['actor'], fact['account'], fact['conversation']) + ':' + fact['attribute']


def retired(store, identifier):
    return store.state(PREFIX + identifier)


def accepts(store, event, attribute):
    key = scope(store, event.actor.key, event.account, event.thread) + ':' + attribute
    stopped = store.state(GUARD + key)
    return stopped is None or datetime.fromisoformat(event.occurred_at) > datetime.fromisoformat(stopped['at'])


def preview(store, identifier):
    chosen = store.read('memories', identifier)
    fact = chosen['data'].get('fact')
    if not fact:
        raise AssistantError('invalid_query', '请选择个人事实')
    key = identity(store, fact)
    with store.db() as db:
        rows = db.execute("SELECT i.id,i.version,p.version AS policy_version,p.valid FROM personal_fact_index i "
                          "JOIN content_records c USING(kind,id) LEFT JOIN governance_policies p "
                          "ON p.resource='memories/' || i.id WHERE i.scope=? AND i.attribute=? ORDER BY i.id",
                          (key.rsplit(':', 1)[0], fact['attribute'])).fetchall()
    snapshot = [{**dict(row), 'retirement': retired(store, row['id'])} for row in rows]
    signature = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {'fingerprint': signature, 'rows': snapshot, 'scope': key,
            'affected_count': len(rows), 'active_count': sum(row['retirement'] is None for row in snapshot)}


def retire(store, identifier, expected, actor, *, clock=lambda: datetime.now(timezone.utc)):
    with store.lock, store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        plan = preview(store, identifier)
        if plan['fingerprint'] != expected:
            raise AssistantError('edit_conflict', '事实或停用状态已改变，请重新查看影响')
        if not plan['active_count']:
            return {'retired': True, 'already_retired': True, 'history_retained': True, 'count': 0}
        now = clock().isoformat()
        repo = GovernanceRepository(store)
        for row in plan['rows']:
            if row['retirement'] is not None:
                continue
            policy = repo.policy('memories/' + row['id'])
            if policy and policy.valid:
                repo.set_policy(replace(policy, valid=False, version=secrets.token_hex(16)), policy.version, actor, db=db)
            store.set_state(PREFIX + row['id'], {'at': now, 'scope': plan['scope']}, db)
        # One compact watermark per subject/account/conversation/attribute.
        # It survives body cleanup, so replaying an old event cannot recreate it.
        store.set_state(GUARD + plan['scope'], {'at': now}, db)
        repo.audit(actor, 'fact_retired', 'memories/' + identifier, db=db)
        return {'retired': True, 'history_retained': True, 'count': plan['active_count']}


def archived_history(store, identifier, *, after='', limit=20, at=None):
    """Local administrator inspection; never used by a model retrieval path."""
    marker = retired(store, identifier)
    if marker is None:
        return None
    if at is not None:
        raise AssistantError('invalid_query', '已停用事实按停用历史展示，请清空历史时点后读取')
    with store.db() as db:
        rows = db.execute("SELECT i.id,i.version FROM personal_fact_index i JOIN content_records c USING(kind,id) "
                          "WHERE i.scope=? AND i.attribute=? ORDER BY i.id",
                          tuple(marker['scope'].rsplit(':', 1))).fetchall()
    retained = [(row, retired(store, row['id'])) for row in rows]
    retained = [(row, status) for row, status in retained if status is not None]
    ids = [row['id'] for row, _ in retained]
    if after and after not in ids:
        raise AssistantError('edit_conflict', '历史列表已改变，请重新读取')
    offset = ids.index(after) + 1 if after else 0
    page = retained[offset:offset + limit]
    items = []
    for row, status in page:
        data = store.read('memories', row['id'])['data']
        items.append({'id': row['id'], 'version': row['version'], **data['fact'], 'source': data['source'],
                      'status': 'disabled', 'retired_at': status['at']})
    signature = [(dict(row), status) for row, status in retained]
    return {'items': items, 'revision': hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest(),
            'total': len(ids), 'counts': {'active': 0, 'disputed': 0, 'historical': 0, 'future': 0, 'disabled': len(ids)},
            'at': datetime.now(timezone.utc).isoformat(),
            'next': page[-1][0]['id'] if offset + limit < len(ids) else None}


def cleanup(store, *, guard=lambda: None, clock=lambda: datetime.now(timezone.utc), limit=50, reclaim_snapshots=False):
    """Small resumable batches; superseded/unread/unreferenced alone never qualifies."""
    cutoff = clock() - timedelta(days=settings(store)['days'])
    with store.lock, store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        guard()
        from .source_retention import INDEX, backfill, held

        ready = backfill(store, db)
        if not ready and store.state(INDEX)['stage'] == 'state':
            ready = backfill(store, db)
        snapshots = {'examined': 0, 'removed': 0, 'retained': 0, 'status': 'indexing' if not ready else 'not_requested'}
        if ready and reclaim_snapshots:
            from .fact_snapshot_cleanup import collect

            snapshots = collect(store, db, guard=guard, now=clock().timestamp(), limit=limit)
        cursor = store.state('fact-cleanup-cursor', '')
        rows = db.execute('SELECT key FROM state WHERE key LIKE ? AND key>? ORDER BY key LIMIT ?',
                          (PREFIX + '%', cursor, limit)).fetchall() if ready else []
        removed = blocked = 0
        for row in rows:
            marker = store.state(row['key'])
            if datetime.fromisoformat(marker['at']) > cutoff:
                continue
            identifier = row['key'][len(PREFIX):]
            resource = 'memories/' + identifier
            # Kept history and durable task/trace references still prevent removal.
            if held(db, resource) or db.execute('SELECT 1 FROM source_dependencies WHERE source=? LIMIT 1', (resource,)).fetchone():
                blocked += 1
                continue
            policy = GovernanceRepository(store).policy(resource)
            if policy is not None and policy.valid:
                blocked += 1
                continue
            guard()
            db.execute("DELETE FROM content_history WHERE kind='memories' AND id=?", (identifier,))
            db.execute("DELETE FROM content_records WHERE kind='memories' AND id=?", (identifier,))
            db.execute('DELETE FROM source_dependencies WHERE derived=?', (resource,))
            db.execute('DELETE FROM record_owners WHERE resource=?', (resource,))
            db.execute("UPDATE governance_policies SET sources='[]',source_versions='[]',owners='[]',conversation='' "
                       "WHERE resource=? AND valid=0", (resource,))
            db.execute('DELETE FROM state WHERE key=?', (row['key'],))
            # Keep the revoked policy and compact scope watermark: old source pins
            # still fail, and no historical value is reinstated by removing a body.
            removed += 1
        # A pass that changed nothing must not rewrite its own bookkeeping: `set_state`
        # stores a fresh nonce, and the portable preview compares a store fingerprint, so a
        # write with no semantic change would reject a switch nobody changed.
        cursor = rows[-1]['key'] if len(rows) == limit else ''
        if store.state('fact-cleanup-cursor', '') != cursor:
            store.set_state('fact-cleanup-cursor', cursor, db)
        result = {'examined': len(rows), 'removed': removed, 'referenced_or_active': blocked, 'snapshots': snapshots}
        previous = store.state('fact-cleanup-last')
        snapshots_removed = snapshots.get('removed', 0) if isinstance(snapshots, dict) else 0
        # A batch that really deleted something always records its own time, even when two
        # consecutive batches happen to remove the same number: `facts.retention` renders
        # `最近一批检查 …` from this record. A pass that removed nothing and changed nothing
        # stays silent, so an idle maintenance pass cannot change a store fingerprint.
        if (not isinstance(previous, dict)
                or removed or snapshots_removed
                or {key: value for key, value in previous.items() if key != 'at'} != result):
            store.set_state('fact-cleanup-last', {**result, 'at': clock().isoformat()}, db)
        guard()
        return result
