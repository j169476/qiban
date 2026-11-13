"""Transactional references outside policy DAGs; resumable legacy indexing."""
INDEX = 'source-retention-index-v1'
VIEW = 'personal-fact-view/'


def tracked(key):
    return key == 'seed-owner-claim' or key.startswith(('reply-trace:', 'action-provenance:'))


def replace(db, kind, identifier, resources):
    db.execute('DELETE FROM source_retention WHERE owner_kind=? AND owner_id=?', (kind, identifier))
    values = sorted({r for r in resources if r == '*' or r.startswith((VIEW, 'memories/'))})
    if values:
        db.executemany('INSERT INTO source_retention VALUES(?,?,?)', [(kind, identifier, r) for r in values])


def state_refs(db, key, value):
    if key == 'seed-owner-claim':
        resources = [p['kind'] + '/' + p['id'] for p in value.get('pins', ())
                     if p.get('id')] if value.get('pending') else []
    elif key.startswith('action-provenance:'):
        resources = [r for r, _ in (value or {}).get('pins', ())]
    elif key.startswith('reply-trace:'):
        resources = [s['resource'] for s in value.get('sources', ())]
        if 'retention_roots' in value:
            resources += value['retention_roots']
        elif value.get('sources_limited'):
            # Wait for old truncated diagnostics to expire; never guess omissions.
            resources += ['*']
    else:
        return
    replace(db, 'state', key, resources)


def action_refs(db, identifier, body):
    replace(db, 'core_action', identifier, [r for r, _ in (body.get('disclosure') or {}).get('pins', ())])


def backfill(store, db, limit=100):
    progress = store.state(INDEX, {'stage': 'actions', 'after': ''})
    if progress['stage'] == 'ready':
        return True
    if progress['stage'] == 'actions':
        rows = db.execute('SELECT id,body FROM core_actions WHERE id>? ORDER BY id LIMIT ?',
                          (progress['after'], limit)).fetchall()
        for row in rows:
            action_refs(db, row['id'], store.ledger_decode('core_actions.body', row['body']))
        progress = ({'stage': 'actions', 'after': rows[-1]['id']} if len(rows) == limit else
                    {'stage': 'state', 'after': ''})
    else:
        rows = db.execute("SELECT key,value FROM state WHERE key>? AND "
                          "(key LIKE 'reply-trace:%' OR key LIKE 'action-provenance:%' OR key='seed-owner-claim') ORDER BY key LIMIT ?",
                          (progress['after'], limit)).fetchall()
        for row in rows:
            state_refs(db, row['key'], store.ledger_decode('state:' + row['key'], row['value']))
        progress = ({'stage': 'state', 'after': rows[-1]['key']} if len(rows) == limit else
                    {'stage': 'ready', 'after': ''})
    store.set_state(INDEX, progress, db)
    return progress['stage'] == 'ready'


def held(db, resource):
    return bool(db.execute("SELECT 1 FROM source_retention WHERE resource IN (?, '*') LIMIT 1", (resource,)).fetchone())
