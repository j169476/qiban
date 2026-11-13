"""Measure provenance growth without deleting any facts or historical views."""
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from companion_core import events
from companion_core.access import RecordPolicy
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.governance import GovernedContext
from companion_core.temporal_facts import PersonalFacts
from tests_postgres.test_fact_trace_governance import facts
from tests_postgres.test_personal_fact_management import seed_chain


def snapshot(store):
    with store.db() as db:
        return {
            'facts': db.execute("SELECT COUNT(*) FROM personal_fact_index").fetchone()[0],
            'views': db.execute("SELECT COUNT(*) FROM governance_policies WHERE resource LIKE 'personal-fact-view/%'").fetchone()[0],
            'view_edges': db.execute("SELECT COUNT(*) FROM source_dependencies WHERE derived LIKE 'personal-fact-view/%'").fetchone()[0],
            'view_payload_bytes': db.execute("SELECT COALESCE(SUM(octet_length(sources)+octet_length(source_versions)),0) FROM governance_policies WHERE resource LIKE 'personal-fact-view/%'").fetchone()[0],
            'policy_relation_bytes': db.execute("SELECT pg_total_relation_size('governance_policies')").fetchone()[0],
            'dependency_relation_bytes': db.execute("SELECT pg_total_relation_size('source_dependencies')").fetchone()[0],
        }


def logical(value):
    return {key: value[key] for key in ('facts', 'views', 'view_edges', 'view_payload_bytes')}


def test_incremental_projection_growth_and_unchanged_read_reuse(pg_store):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    samples, roots = [], []
    for index in range(128):
        event = Event('alpha', 'bot', f'scale-{index}', ActorRef('alpha', 'a'), 'dm',
                      (start + timedelta(hours=index)).isoformat(), f'我住合成城市{index}')
        events.ingest(pg_store, event)
        governed = GovernedContext(pg_store, event)
        governed.classify_event()
        identifier = f'fact-scale-{index:04}'
        pg_store.write('memories', {'schema_version': 1, 'id': identifier, 'kind': 'user_preference',
            'content': event.text, 'confidence': 'explicit', 'tags': ['personal_fact'], 'locked': False,
            'source': {'type': 'interaction', 'recorded_at': event.occurred_at, 'reference': event.id},
            'fact': {'actor': event.actor.key, 'account': event.account, 'conversation': event.thread,
                     'attribute': 'residence', 'value': f'合成城市{index}', 'quote': event.text,
                     'valid_from': event.occurred_at, 'supersedes': []}}, None, 'synthetic-scale', identifier)
        governed.classify_written('memories', identifier, personal=True, sources=['event/' + event.id])
        projection = PersonalFacts(governed, event, lambda: datetime.now(timezone.utc))
        projection.freeze_projection()
        roots.append(next(key for key in governed.resources if key.startswith('personal-fact-view/')))
        if index + 1 in (32, 64, 128):
            samples.append(snapshot(pg_store))
    before = snapshot(pg_store)
    for _ in range(8):
        projection.freeze_projection()
    after = snapshot(pg_store)
    assert logical(before) == logical(after)
    assert len(set(roots)) == 128 and after['facts'] == 128
    # A later read must not rewrite any older source snapshot.
    for root in roots[::16]:
        governed.access.lease(governed.request, [root])
    report = {'samples': samples, 'unchanged_reads_no_logical_growth': True,
              'historical_snapshots_readable': True, 'retained_facts': after['facts']}
    if os.environ.get('QIBAN_DESKTOP_TEST_ROOT'):
        path = Path(os.environ['QIBAN_DESKTOP_TEST_ROOT']).parent / 'fact-projection-growth.json'
        path.write_text(json.dumps(report, indent=2), encoding='utf8')
    assert after['view_edges'] < 128 * 40  # Flat snapshots require 8,256 edges.


def projection(store):
    seed_chain(store, 48)
    event = Event('alpha', 'bot', 'projection-check', ActorRef('alpha', 'a'), 'dm',
                  datetime.now(timezone.utc).isoformat(), '住所')
    events.ingest(store, event)
    governed = GovernedContext(store, event)
    governed.classify_event()
    value = PersonalFacts(governed, event, lambda: datetime.now(timezone.utc))
    return event, governed, value


def test_existing_flat_snapshot_is_not_rewritten(pg_store):
    event, governed, value = projection(pg_store)
    pins = [('memories/' + h['id'], governed.repository.policy('memories/' + h['id']).version)
            for h in sorted(value.headers, key=lambda h: h['id'])]
    identifier = stable_key('fact-view', event.actor.key, event.account, event.thread, value.signature, pins)
    old = RecordPolicy('personal-fact-view/' + identifier, identifier, 'personal', frozenset((event.actor.key,)),
                       event.thread, 'private', tuple(r for r, _ in pins), source_versions=tuple(pins))
    governed.repository.set_policy(old, None, 'synthetic-old-release')
    before = snapshot(pg_store)
    value.freeze_projection()
    assert governed.repository.policy(old.resource) == old
    assert logical(snapshot(pg_store)) == logical(before)
    governed.access.lease(governed.request, [old.resource])


def test_shared_projection_preserves_all_sources_and_revocation_after_backup(pg_store):
    from companion_core.postgres_transfer import create, fingerprint, restore

    _, governed, value = projection(pg_store)
    value.freeze_projection()
    root = next(r for r in governed.resources if r.startswith('personal-fact-view/'))
    lease = governed.access.lease(governed.request, [root])
    resources = {r for r, _ in lease.versions}
    assert {f'memories/fact-history-{i:04}' for i in range(48)} <= resources
    before = snapshot(pg_store)
    blob = create(pg_store, pg_store._backup_key)
    source = governed.repository.policy('memories/fact-history-0000')
    governed.repository.set_policy(replace(source, version='revoked', valid=False), source.version, 'test')
    with pytest.raises(AssistantError):
        governed.access.revalidate(lease)
    restore(pg_store, blob, pg_store._backup_key, expected=fingerprint(pg_store))
    assert snapshot(pg_store)['view_edges'] == before['view_edges']
    # Restore invalidates sessions; obtain a fresh authorized reading.
    _, restored, _ = facts(pg_store, 'read-restored-tree', '住所')
    assert root in restored.resources
    restored.access.lease(restored.request, [root])


def test_partial_branch_write_rolls_back_and_retry_does_not_duplicate(pg_store, monkeypatch):
    _, governed, value = projection(pg_store)
    original = governed.repository.set_policy
    writes = 0

    def interrupted(*args, **kwargs):
        nonlocal writes
        result = original(*args, **kwargs)
        writes += 1
        if writes == 2:
            raise RuntimeError('synthetic branch write interruption')
        return result

    monkeypatch.setattr(governed.repository, 'set_policy', interrupted)
    with pytest.raises(RuntimeError, match='synthetic'):
        value.freeze_projection()
    assert snapshot(pg_store)['views'] == snapshot(pg_store)['view_edges'] == 0
    monkeypatch.setattr(governed.repository, 'set_policy', original)
    value.freeze_projection()
    before = snapshot(pg_store)
    value.freeze_projection()
    assert logical(snapshot(pg_store)) == logical(before)


def test_shared_branch_change_cannot_silently_repair_provenance(pg_store):
    _, governed, value = projection(pg_store)
    value.freeze_projection()
    root = next(r for r in governed.resources if r.startswith('personal-fact-view/'))
    policy = governed.repository.policy(root)
    branch = next(r for r in policy.sources if r.startswith('personal-fact-view/'))
    old = governed.repository.policy(branch)
    governed.repository.set_policy(replace(old, version='revoked-branch', valid=False), old.version, 'test')
    with pytest.raises(AssistantError, match='来源节点'):
        value.freeze_projection()
    assert governed.repository.policy(branch).version == 'revoked-branch'
