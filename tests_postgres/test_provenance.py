from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core.access import AccessService, GraphLimits, RecordPolicy
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import AdminService, GovernedContext, record_provenance
from companion_core.postgres_transfer import create, fingerprint, restore
from companion_core.provenance import freeze, rebase, routine_update, scope_id
from companion_core.public_workspace import PublicWorkspace
from companion_core.verification import VerificationService
from companion_service.admin import Management

OWNER, OTHER = ActorRef("alpha", "a"), ActorRef("beta", "b")
RESOURCE = "relationships/" + OWNER.key


def test_capacity_observer_does_not_accumulate_ctypes_types():
    import ctypes

    from scripts.benchmark_metrics import rss

    rss()  # Initialize the platform API once.
    before = len(ctypes._pointer_type_cache)
    for _ in range(100):
        measurement = rss()
        assert 0 < measurement["rss_bytes"] <= measurement["process_peak_rss_bytes"]
    assert len(ctypes._pointer_type_cache) <= before + 2


def request(admin, actor=OWNER, *, administrator=False, audience="private"):
    return admin.authority.issue(actor=actor, conversation="private-a", audience=audience,
                                recipients=(actor,) if audience == "private" else (),
                                purpose="management" if administrator else "reply", administrator=administrator)


def aggregate(store, *, source=False, frozen=True):
    data = {"schema_version": 1, "id": OWNER.key, "display_name": "synthetic",
            "affinity": 50, "interactions": 0, "events": [], "locked": False}
    store.write("relationships", data, None, "fixture", OWNER.key)
    admin = AdminService(store)
    repo = admin.repository
    if source:
        repo.set_policy(RecordPolicy("event/source", "v1", "personal", frozenset((OWNER.key,))), None, "fixture")
    admin.classify(request(admin, administrator=True), resource=RESOURCE, expected=repo.policy(RESOURCE).version,
                   classification="personal", owners=[OWNER.key], conversation="private-a",
                   sources=["event/source"] if source else [])
    if not frozen:
        return admin, None, None, None
    live, pins = freeze(admin.access, admin.access.lease(request(admin), [RESOURCE]))
    record_provenance(store, "memories", "derived", {"owners": [OWNER.key], "conversation": "private-a",
                                                    "audience": "private", "pins": pins})
    assert admin.access.can_read(request(admin), "memories/derived")
    snapshot = next(key for key, _ in pins if key.startswith("provenance/snapshot/"))
    return admin, live, pins, snapshot


def change(store, admin):
    item = store.read("relationships", OWNER.key)
    with routine_update(store, RESOURCE):
        store.write("relationships", {**item["data"], "affinity": item["data"]["affinity"] + 1},
                    item["version"], "fixture-core", OWNER.key)
    return admin.repository.policy(RESOURCE)


def test_freezing_deep_mutable_chain_is_iterative_and_keeps_live_revocation(pg_store):
    admin = AdminService(pg_store)
    previous = None
    with pg_store.db():
        for index in range(1100):
            identifier = ActorRef("synthetic", str(index)).key
            pg_store.write("relationships", {"schema_version": 1, "id": identifier, "display_name": "synthetic",
                "affinity": 50, "interactions": 0, "events": [], "locked": False}, None, "fixture", identifier)
            resource = "relationships/" + identifier
            old = admin.repository.policy(resource)
            pins = ((previous.resource, previous.version),) if previous else ()
            current = RecordPolicy(resource, "v" + str(index), "personal", frozenset((OWNER.key,)),
                                   sources=tuple(key for key, _ in pins), source_versions=pins)
            admin.repository.set_policy(current, old.version, "fixture")
            previous = current
    live, pins = freeze(admin.access, admin.access.lease(request(admin), [previous.resource]))
    assert len(live.versions) == 3300
    frozen = admin.access.lease(request(admin), [key for key, _ in pins])
    assert len(frozen.versions) == 2200
    first = "relationships/" + ActorRef("synthetic", "0").key
    old = admin.repository.policy(first)
    admin.repository.set_policy(replace(old, version="revoked", valid=False), old.version, "fixture")
    with pytest.raises(AssistantError):
        admin.access.revalidate(frozen)


def test_snapshot_is_metadata_only_immutable_and_live_lease_still_pins_reads(pg_store):
    admin, live, pins, snapshot = aggregate(pg_store)
    old = admin.repository.policy(snapshot)
    latest = change(pg_store, admin)
    assert admin.repository.policy(snapshot) == old
    assert admin.access.can_read(request(admin), "memories/derived")
    with pytest.raises(AssistantError):
        admin.access.revalidate(live)  # A snapshot does not excuse an unrelated write in flight.
    updated = rebase(admin.access, live, RESOURCE, latest.version, pins)
    admin.access.revalidate(updated)
    with pg_store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM governance_provenance").fetchone()[0] == 2
        assert not db.execute("SELECT 1 FROM content_records WHERE id=?", (snapshot,)).fetchone()
    with pytest.raises(AssistantError, match="历史来源"):
        admin.repository.set_policy(replace(old, version="forged"), old.version, "admin")


def test_disclosure_batch_never_reuses_previous_check_and_fails_closed_on_damaged_index(pg_store):
    admin, _, _, _ = aggregate(pg_store, source=True)
    lease = admin.access.lease(request(admin), ["memories/derived"])
    # The index may choose records, but never replaces checking actual source pins.
    with pg_store.db() as db:
        db.execute("DELETE FROM source_dependencies WHERE source=?", ("event/source",))
    assert not admin.access.can_read(request(admin), "memories/derived")
    # A lease already carries every required source as a root: it can still be
    # checked from authoritative policies without trusting the damaged index.
    admin.access.revalidate(lease)
    source = admin.repository.policy("event/source")
    admin.repository.set_policy(replace(source, version="changed", valid=False), source.version, "admin")
    assert not admin.access.can_read(request(admin), "memories/derived")
    with pytest.raises(AssistantError):
        admin.access.revalidate(lease)


def test_snapshot_existence_is_batched_and_body_deletion_still_revokes(pg_store, monkeypatch):
    admin, _, _, snapshot = aggregate(pg_store, source=True)

    def per_record_query(*args):
        raise AssertionError("one existence query per provenance target would make deep graphs time out")

    monkeypatch.setattr(pg_store, "exists", per_record_query)
    lease = admin.access.lease(request(admin), [snapshot])
    admin.access.revalidate(lease)
    with pg_store.db() as db:
        db.execute("DELETE FROM content_records WHERE kind='relationships' AND id=?", (OWNER.key,))
    with pytest.raises(AssistantError):
        admin.access.revalidate(lease)


def test_disclosure_metadata_releases_without_waiting_for_cyclic_gc(pg_store):
    import gc
    import weakref

    admin, _, _, snapshot_id = aggregate(pg_store, source=True)
    enabled = gc.isenabled()
    try:
        gc.disable()
        snapshot = admin.repository.disclosure_snapshot([snapshot_id], 10_000)
        reference = weakref.ref(snapshot.policy(snapshot_id))
        assert reference() is not None
        del snapshot
        assert reference() is None  # Long replies must not retain every prior complete source graph.
    finally:
        if enabled:
            gc.enable()


@pytest.mark.parametrize("operation", ["restrict", "same_classification", "owners", "invalid", "expiration",
                                       "admin_correct", "direct_write", "delete_policy", "delete_body", "source_change"])
def test_every_revocation_and_correction_invalidates_historical_evidence(pg_store, operation):
    admin, _, _, snapshot = aggregate(pg_store, source=True)
    repo = admin.repository
    if operation in ("restrict", "same_classification", "owners", "invalid"):
        admin.classify(request(admin, administrator=True), resource=RESOURCE, expected=repo.policy(RESOURCE).version,
                       classification="restricted" if operation == "restrict" else "personal",
                       owners=[OTHER.key if operation == "owners" else OWNER.key], conversation="private-a",
                       valid=operation != "invalid", sources=["event/source"])
    elif operation == "expiration":
        old = repo.policy(RESOURCE)
        repo.set_policy(replace(old, version="expired", expires_at=1), old.version, "admin")
    elif operation in ("admin_correct", "direct_write"):
        item = pg_store.read("relationships", OWNER.key)
        corrected = {**item["data"], "display_name": "corrected"}
        if operation == "admin_correct":
            management = Management(SimpleNamespace(store=pg_store, admin=admin))
            management.correct(request(admin, administrator=True), "relationships", OWNER.key,
                               corrected, item["version"])
        else:
            # An actor string that imitates the core is not a preservation capability.
            pg_store.write("relationships", corrected, item["version"], "core-provenance", OWNER.key)
    elif operation == "source_change":
        old = repo.policy("event/source")
        repo.set_policy(replace(old, version="corrected"), old.version, "admin")
    else:
        with pg_store.db() as db:
            if operation == "delete_policy":
                db.execute("DELETE FROM governance_policies WHERE resource=?", (RESOURCE,))
            else:
                db.execute("DELETE FROM content_records WHERE kind=? AND id=?", ("relationships", OWNER.key))
    assert not admin.access.can_read(request(admin), snapshot)
    assert not admin.access.can_read(request(admin), "memories/derived")


def test_permission_change_even_inside_routine_context_revokes_snapshots(pg_store):
    admin, _, _, snapshot = aggregate(pg_store)
    with routine_update(pg_store, RESOURCE):
        old = admin.repository.policy(RESOURCE)
        admin.repository.set_policy(replace(old, version="changed-scope", audience="group"), old.version, "test")
    assert not admin.access.can_read(request(admin), snapshot)


def test_recreating_deleted_relationship_cannot_revive_old_snapshots(pg_store):
    admin, _, _, snapshot = aggregate(pg_store)
    data = pg_store.read("relationships", OWNER.key)["data"]
    with pg_store.db() as db:
        db.execute("DELETE FROM content_records WHERE kind=? AND id=?", ("relationships", OWNER.key))
    assert not admin.access.can_read(request(admin), snapshot)
    with routine_update(pg_store, RESOURCE):
        pg_store.write("relationships", data, None, "test", OWNER.key)
    assert not admin.access.can_read(request(admin), snapshot)
    assert not admin.access.can_read(request(admin), "memories/derived")


def test_cross_account_grants_are_exact_revocable_and_never_transfer_to_public_or_group(pg_store):
    admin, _, _, snapshot = aggregate(pg_store)
    assert not admin.access.can_read(request(admin, OTHER), snapshot)
    grant = admin.authorize(request(admin, administrator=True), grantee=OTHER.key, owner=OWNER.key,
                            resources=[RESOURCE])
    lease = admin.access.lease(request(admin, OTHER), [snapshot])
    assert admin.access.can_read(request(admin, OWNER), snapshot)
    for actor in (OWNER, OTHER):
        for audience in ("group", "public"):
            assert not admin.access.can_read(request(admin, actor, audience=audience), snapshot)
    for virtual in (snapshot, scope_id(RESOURCE)):
        with pytest.raises(AssistantError):
            admin.authorize(request(admin, administrator=True), grantee=OTHER.key, owner=OWNER.key, resources=[virtual])
        with pytest.raises(AssistantError):
            VerificationService(admin.repository, admin.authority).begin(request(admin), OTHER, [virtual])
    admin.revoke(request(admin, administrator=True), grant["id"])
    with pytest.raises(AssistantError):
        admin.access.revalidate(lease)
    assert not admin.access.can_read(request(admin, OTHER), snapshot)


def test_own_rebase_cannot_accept_unrelated_change_or_new_dependency(pg_store):
    admin, live, pins, _ = aggregate(pg_store, source=True)
    written = change(pg_store, admin)
    source = admin.repository.policy("event/source")
    admin.repository.set_policy(replace(source, version="v2"), source.version, "other-entry")
    with pytest.raises(AssistantError):
        rebase(admin.access, live, RESOURCE, written.version, pins)
    # Even an accessible new source is not evidence that the original model saw.
    repo = admin.repository
    live, pins = freeze(admin.access, admin.access.lease(request(admin), ["event/source"]))
    repo.set_policy(RecordPolicy("new-source", "new", "public"), None, "test")
    repo.set_policy(replace(repo.policy("event/source"), version="v3", sources=("new-source",),
                            source_versions=(("new-source", "new"),)), "v2", "test")
    with pytest.raises(AssistantError):
        rebase(admin.access, live, "event/source", "v3", pins)


@pytest.mark.parametrize("size,allowed", [(256, True), (257, False)])
def test_direct_source_limit_stays_256_and_snapshot_nodes_use_graph_budget(pg_store, size, allowed):
    admin, _, _, _ = aggregate(pg_store, frozen=False)
    with pg_store.db() as db:
        for index in range(size - 1):
            admin.repository.set_policy(RecordPolicy("node/" + str(index), "v1", "public"), None, "test", db=db)
    resources = [RESOURCE, *("node/" + str(index) for index in range(size - 1))]
    assert admin.access.max_sources == 256
    if allowed:
        live, pins = freeze(admin.access, admin.access.lease(request(admin), resources))
        assert len(live.versions) == size + 2
        assert len(pins) == size
    else:
        with pytest.raises(AssistantError):
            admin.access.lease(request(admin), resources)
        with pg_store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM governance_provenance").fetchone()[0] == 0


def test_snapshot_creation_rolls_back_when_separate_graph_budget_is_exhausted(pg_store):
    admin, _, _, _ = aggregate(pg_store, frozen=False)
    access = AccessService(admin.repository, admin.authority, limits=GraphLimits(nodes=2))
    with pytest.raises(AssistantError):
        freeze(access, access.lease(request(admin), [RESOURCE]))
    with pg_store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM governance_provenance").fetchone()[0] == 0


def test_redundant_edges_are_removed_but_all_ancestors_and_revocation_are_preserved(pg_store):
    admin, _, _, _ = aggregate(pg_store, source=True, frozen=False)
    repo = admin.repository
    ctx = request(admin)
    live, pins = freeze(admin.access, admin.access.lease(ctx, [RESOURCE, "event/source"]))
    assert len(pins) == 1  # event/source is retained through the aggregate snapshot.
    frozen = admin.access.lease(ctx, [key for key, _ in pins])
    assert "event/source" in dict(frozen.versions)
    assert {key for key, _ in frozen.versions} == {key for key, _ in live.versions} - {RESOURCE}
    source = repo.policy("event/source")
    repo.set_policy(replace(source, version="revoked", valid=False), source.version, "test")
    with pytest.raises(AssistantError):
        admin.access.revalidate(frozen)


def test_cycle_and_unregistered_virtual_policy_fail_closed(pg_store):
    admin, _, _, _ = aggregate(pg_store)
    with pytest.raises(AssistantError):
        admin.classify(request(admin, administrator=True), resource="provenance/snapshot/forged",
                       expected=None, classification="public")
    # Simulate malformed imported metadata: a prefix alone must not grant access.
    with pg_store.db() as db:
        db.execute("INSERT INTO governance_policies VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("provenance/snapshot/forged", "v1", "public", "[]", "", "public", "[]", 1, None, "[]"))
    assert not admin.access.can_read(request(admin), "provenance/snapshot/forged")
    admin.repository.set_policy(RecordPolicy("cycle-node", "loop", "public"), None, "test")
    current = admin.repository.policy(RESOURCE)
    admin.repository.set_policy(replace(current, version="cycle", sources=("cycle-node",),
                                        source_versions=(("cycle-node", "loop"),)), current.version, "test")
    admin.repository.set_policy(RecordPolicy("cycle-node", "loop", "public", sources=(RESOURCE,),
                                             source_versions=((RESOURCE, "cycle"),)), "loop", "test")
    with pytest.raises(AssistantError):
        freeze(admin.access, admin.access.lease(request(admin), [RESOURCE]))


def test_malformed_snapshot_cannot_drop_its_live_revocation_dependency(pg_store):
    admin, _, _, snapshot = aggregate(pg_store)
    with pg_store.db() as db:
        # Deliberately malformed imported row retains pins but drops the edge.
        db.execute("UPDATE governance_policies SET sources='[]' WHERE resource=?", (snapshot,))
    assert not admin.access.can_read(request(admin), snapshot)
    assert snapshot not in admin.repository.readable_resources(admin.access, request(admin))


@pytest.mark.parametrize("mutation", ["routine", "direct", "classification", "delete_recreate"])
def test_public_growth_cycles_preserve_notes_but_administration_revokes_them(pg_store, mutation):
    for index in range(3):
        workspace = PublicWorkspace(pg_store)
        growth = workspace.read("growth")
        workspace.before_model({"growth": growth["data"]})
        workspace.write("growth", growth["data"], growth["version"], "test")
        note = {"schema_version": 1, "id": "public-" + str(index), "kind": "experience", "content": "public-only",
                "source": {"type": "interaction", "reference": "test", "recorded_at": "2026-10-23T00:00:00Z"},
                "confidence": "observed", "tags": [], "locked": False}
        workspace.write("memories", note, None, "test", note["id"])
        workspace.check()
        if index >= 1:
            assert workspace.access.can_read(workspace.request, "memories/public-1")
    if mutation == "direct":
        pg_store.set_state("public-growth", pg_store.state("public-growth"))
    elif mutation == "classification":
        policy = workspace.repository.policy("partition/public-growth")
        workspace.repository.set_policy(replace(policy, version="admin"), policy.version, "admin")
    elif mutation == "delete_recreate":
        previous = pg_store.state("public-growth")
        with pg_store.db() as db:
            db.execute("DELETE FROM state WHERE key='public-growth'")
        with routine_update(pg_store, "partition/public-growth"):
            pg_store.set_state("public-growth", previous)
    allowed = mutation == "routine"
    assert workspace.access.can_read(workspace.request, "memories/public-1") == allowed
    assert workspace.access.can_read(workspace.request, "memories/public-2") == allowed


def test_snapshot_metadata_restores_with_revocation_and_no_implicit_role_start(pg_store):
    admin, _, _, snapshot = aggregate(pg_store)
    grant = admin.authorize(request(admin, administrator=True), grantee=OTHER.key, owner=OWNER.key, resources=[RESOURCE])
    key = b"s" * 32
    backup = create(pg_store, key)
    admin.revoke(request(admin, administrator=True), grant["id"])
    restore(pg_store, backup, key, expected=fingerprint(pg_store))
    assert admin.access.can_read(request(admin), snapshot)
    assert not admin.access.can_read(request(admin, OTHER), snapshot)
    assert pg_store.state("core-run", {}).get("status") != "active"


def test_routine_context_does_not_leak_after_error_and_transaction_rolls_back(pg_store):
    admin, live, _, _ = aggregate(pg_store)
    before = pg_store.read("relationships", OWNER.key)
    scope = admin.repository.policy(scope_id(RESOURCE))
    with pytest.raises(RuntimeError):
        with pg_store.lock, pg_store.db(), routine_update(pg_store, RESOURCE):
            pg_store.write("relationships", {**before["data"], "affinity": 51}, before["version"], "test", OWNER.key)
            raise RuntimeError("synthetic failure before provenance commit")
    assert pg_store.read("relationships", OWNER.key) == before
    assert admin.repository.policy(scope_id(RESOURCE)) == scope
    admin.access.revalidate(live)
    pg_store.write("relationships", before["data"], before["version"], "test", OWNER.key)
    assert not admin.access.can_read(request(admin), "memories/derived")


def test_governed_private_turn_freezes_only_approved_aggregate_types(pg_store):
    admin, _, _, _ = aggregate(pg_store)
    event = Event("alpha", "bot", "event", OWNER, "private-a", "2026-10-23T00:00:00Z", "test", "private")
    governed = GovernedContext(pg_store, event)
    governed.read("relationships", OWNER.key)
    governed.freeze()
    assert RESOURCE not in dict(governed.proof()["pins"])
    assert RESOURCE in dict(governed.lease.versions)
    with pytest.raises(AssistantError):
        with routine_update(pg_store, "memories/derived"):
            pass
