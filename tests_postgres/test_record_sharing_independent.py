"""Codex independent acceptance: synthetic data only, no model or connector."""
import json
from dataclasses import replace

from tests_postgres.test_desktop_record_sharing import (
    BOB,
    CAROL,
    can_read,
    memory,
    register,
    service_for,
    set_policy,
)
from tests_postgres.test_desktop_reply_quota import call, result


def test_local_owner_can_reclassify_without_becoming_an_external_contact(pg_store):
    service = service_for(pg_store)
    memory(service, 'local-owned')
    previous = set_policy(service, 'memories/local-owned', owners={service.actor.key})
    proposal = result(service, 'sharing.preview_classify', {
        'kind': 'memories', 'identifier': 'local-owned', 'classification': 'public'})
    result(service, 'sharing.commit', {'token': proposal['token']})
    saved = service.repository.policy(previous.resource)
    assert saved.classification == 'public'
    assert replace(saved, version=previous.version, classification=previous.classification) == previous


def test_maintenance_only_is_reported_as_effective_without_enabling_reply(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, 'maintenance-only')
    set_policy(service, 'memories/maintenance-only', classification='restricted')
    proposal = result(service, 'sharing.preview_grant', {
        'kind': 'memories', 'identifier': 'maintenance-only', 'grantee': BOB.key,
        'owner': service.actor.key, 'purposes': ['maintenance'], 'days': 30})
    assert proposal['will_apply'] is True
    saved = result(service, 'sharing.commit', {'token': proposal['token']})
    assert saved['effective'] is True
    assert can_read(service, BOB, 'memories/maintenance-only', purpose='maintenance')
    assert not can_read(service, BOB, 'memories/maintenance-only', purpose='reply')


def test_group_to_owner_private_is_not_presented_as_pure_narrowing(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, 'group-origin')
    set_policy(service, 'memories/group-origin', classification='conversation', owners={BOB.key},
               conversation='synthetic-group', audience='group')
    assert not can_read(service, BOB, 'memories/group-origin')
    proposal = result(service, 'sharing.preview_classify', {
        'kind': 'memories', 'identifier': 'group-origin', 'classification': 'personal'})
    assert proposal.get('direction') != '收窄'
    result(service, 'sharing.commit', {'token': proposal['token']})
    assert can_read(service, BOB, 'memories/group-origin')
    assert not can_read(service, BOB, 'memories/group-origin', audience='group', conversation='synthetic-group')


def test_multirecord_grant_revocation_discloses_all_affected_records(pg_store):
    service = service_for(pg_store)
    register(service, BOB, CAROL)
    resources = ['memories/first-shared', 'memories/second-shared']
    for resource in resources:
        memory(service, resource.split('/')[1])
        set_policy(service, resource, classification='restricted')
    granted = service.admin.authorize(service.request(), grantee=BOB.key, owner=service.actor.key,
                                      resources=resources)
    assert all(can_read(service, BOB, resource) for resource in resources)
    proposal = result(service, 'sharing.preview_revoke', {
        'kind': 'memories', 'identifier': 'first-shared', 'grant_id': granted['id']})
    visible = json.dumps(proposal, ensure_ascii=False)
    assert all(resource in visible for resource in resources), 'Revocation must disclose its entire scope'
    result(service, 'sharing.commit', {'token': proposal['token']})
    assert not any(can_read(service, BOB, resource) for resource in resources)


def test_filtered_pages_cannot_hide_later_ordinary_records(pg_store):
    service = service_for(pg_store)
    # Facts belong to their dedicated flow and are skipped by the sharing list.
    for index in range(10):
        identifier = f'a-fact-{index:02}'
        value = {'schema_version': 1, 'id': identifier, 'kind': 'experience', 'content': '合成事实',
                 'confidence': 'observed', 'tags': [], 'locked': False,
                 'fact': {'actor': BOB.key, 'account': 'qq-bot', 'conversation': 'synthetic-private',
                          'attribute': 'residence', 'value': '合成城市', 'quote': '我住在合成城市',
                          'valid_from': '2026-11-08T00:00:00+00:00', 'supersedes': []},
                 'source': {'type': 'user_instruction', 'reference': 'synthetic-independent',
                            'recorded_at': '2026-11-08T00:00:00Z'}}
        pg_store.write('memories', value, None, 'synthetic', identifier)
    memory(service, 'z-ordinary')
    set_policy(service, 'memories/z-ordinary')
    cursor, found = '', []
    for _ in range(15):
        page = result(service, 'sharing.records', {'after': cursor, 'limit': 1})
        found.extend(row['identifier'] for row in page['records'])
        if not page['next']:
            break
        assert page['next'] != cursor
        cursor = page['next']
    assert 'z-ordinary' in found


def test_multirecord_revoke_rechecks_dependents_of_the_other_record(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    resources = ['memories/revoke-root-a', 'memories/revoke-root-b']
    for resource in resources:
        memory(service, resource.split('/')[1])
        set_policy(service, resource, classification='restricted')
    granted = service.admin.authorize(service.request(), grantee=BOB.key, owner=service.actor.key,
                                      resources=resources)
    proposal = result(service, 'sharing.preview_revoke', {
        'kind': 'memories', 'identifier': 'revoke-root-a', 'grant_id': granted['id']})
    # A new dependency of the other granted resource expands the previewed impact.
    memory(service, 'new-dependent')
    parent = service.repository.policy(resources[1])
    set_policy(service, 'memories/new-dependent', classification='public',
               sources=(parent.resource,), pins=((parent.resource, parent.version),))
    response = call(service, 'sharing.commit', {'token': proposal['token']})
    assert not response['ok'] and response['error'] == 'edit_conflict', response
    assert all(can_read(service, BOB, resource) for resource in resources)


def test_cycle_is_reported_as_unusable_source_graph(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    for identifier in ('cycle-a', 'cycle-b'):
        memory(service, identifier)
        set_policy(service, 'memories/' + identifier, classification='public')
    a = service.repository.policy('memories/cycle-a')
    b = service.repository.policy('memories/cycle-b')
    # Same-version writes only construct a synthetic malformed graph for diagnosis.
    service.repository.set_policy(replace(a, sources=(b.resource,), source_versions=((b.resource, b.version),)),
                                  a.version, 'synthetic-cycle')
    service.repository.set_policy(replace(b, sources=(a.resource,), source_versions=((a.resource, a.version),)),
                                  b.version, 'synthetic-cycle')
    detail = result(service, 'sharing.record', {'kind': 'memories', 'identifier': 'cycle-a'})
    assert detail['source']['ok'] is False
    assert not can_read(service, BOB, a.resource)


def test_active_grants_after_the_first_page_remain_manageable(pg_store):
    from tests_postgres.test_desktop_record_sharing import grant_row
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, 'paged-grants')
    resource = 'memories/paged-grants'
    set_policy(service, resource, classification='restricted')
    expected = {grant_row(service, BOB, [resource]).id for _ in range(57)}
    target = {'kind': 'memories', 'identifier': 'paged-grants'}
    detail = result(service, 'sharing.record', target)
    found = {row['id'] for row in detail['grants']}
    cursor = detail['grants_next']
    assert cursor and len(found) <= 50
    for _ in range(5):
        page = result(service, 'sharing.grants', {**target, 'after': cursor})
        ids = {row['id'] for row in page['grants']}
        assert not (ids & found)
        found |= ids
        cursor = page['next']
        if not cursor:
            break
    assert not cursor and found == expected
    later = sorted(expected - {row['id'] for row in detail['grants']})[-1]
    preview = result(service, 'sharing.preview_revoke', {**target, 'grant_id': later})
    assert result(service, 'sharing.commit', {'token': preview['token']})['revoked']


def test_multirecord_revoke_rechecks_other_records_source_state(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    for name in ('source-for-b', 'source-root-a', 'source-root-b'):
        memory(service, name)
        set_policy(service, 'memories/' + name, classification='public')
    source = service.repository.policy('memories/source-for-b')
    set_policy(service, 'memories/source-root-b', classification='public',
               sources=(source.resource,), pins=((source.resource, source.version),))
    resources = ['memories/source-root-a', 'memories/source-root-b']
    granted = service.admin.authorize(service.request(), grantee=BOB.key, owner=service.actor.key,
                                      resources=resources)
    preview = result(service, 'sharing.preview_revoke', {
        'kind': 'memories', 'identifier': 'source-root-a', 'grant_id': granted['id']})
    # Keep the version to ensure source status, not only root versions, is checked.
    service.repository.set_policy(replace(source, valid=False), source.version, 'synthetic')
    response = call(service, 'sharing.commit', {'token': preview['token']})
    assert not response['ok'] and response['error'] == 'edit_conflict', response
    with pg_store.db() as db:
        assert not db.execute('SELECT revoked FROM governance_grants WHERE id=?', (granted['id'],)).fetchone()[0]


def test_multirecord_revoke_rechecks_expiry_of_other_record(pg_store, monkeypatch):
    service = service_for(pg_store)
    register(service, BOB)
    instant = [service.repository.clock()]
    monkeypatch.setattr(service.repository, 'clock', lambda: instant[0])
    monkeypatch.setattr(service.authority, 'clock', lambda: instant[0])
    resources = ['memories/expiry-root-a', 'memories/expiry-root-b']
    for resource in resources:
        memory(service, resource.split('/')[1])
        set_policy(service, resource, classification='public',
                   expires_at=instant[0] + 100 if resource.endswith('-b') else None)
    granted = service.admin.authorize(service.request(), grantee=BOB.key, owner=service.actor.key,
                                      resources=resources)
    preview = result(service, 'sharing.preview_revoke', {
        'kind': 'memories', 'identifier': 'expiry-root-a', 'grant_id': granted['id']})
    instant[0] += 101
    response = call(service, 'sharing.commit', {'token': preview['token']})
    assert not response['ok'] and response['error'] == 'edit_conflict', response


def test_upstream_sources_are_not_mislabelled_as_downstream_impact(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    for name in ('upstream-only', 'source-sharing-target'):
        memory(service, name)
        set_policy(service, 'memories/' + name, classification='public')
    upstream = service.repository.policy('memories/upstream-only')
    target = 'memories/source-sharing-target'
    set_policy(service, target, classification='public', sources=(upstream.resource,),
               pins=((upstream.resource, upstream.version),))
    granted = service.admin.authorize(service.request(), grantee=BOB.key, owner=service.actor.key,
                                      resources=[target])
    preview = result(service, 'sharing.preview_revoke', {
        'kind': 'memories', 'identifier': 'source-sharing-target', 'grant_id': granted['id']})
    assert upstream.resource not in preview['scope_dependents']
    assert preview['scope_dependents_count'] == 0
