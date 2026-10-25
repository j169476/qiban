from dataclasses import replace

import pytest

from companion_core.access import RecordPolicy, RequestAuthority
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import AdminService, ContextService, GovernanceRepository
from companion_core.storage import Store

OWNER = ActorRef("local", "owner", "web")
OTHER = ActorRef("bilibili", "other")


@pytest.fixture
def services(tmp_path):
    store = Store(tmp_path / "data")
    store.initialize()
    repo = GovernanceRepository(store, clock=lambda: 1000)
    repo.initialize()
    authority = RequestAuthority(epoch=repo.epoch, session_active=repo.session_active, clock=lambda: 1000)
    admin = AdminService(store, repo, authority)
    return store, repo, authority, admin


def ctx(authority, actor=OWNER, *, admin=False):
    return authority.issue(actor=actor, conversation="test", audience="private", recipients=(actor,),
                           purpose="management" if admin else "reply", administrator=admin)


def test_admin_frontends_share_versioned_schema_checked_mutation(services):
    store, repo, authority, admin = services
    old = store.read("policy")
    request = ctx(authority, admin=True)
    result = admin.patch_config(request, "policy", {"worker_timeout_seconds": 90}, old["version"])
    assert result == admin.read_config(request, "policy")
    with pytest.raises(AssistantError) as exc:
        admin.patch_config(request, "policy", {"worker_timeout_seconds": 100}, old["version"])
    assert exc.value.code == "edit_conflict"
    with pytest.raises(AssistantError):
        admin.patch_config(ctx(authority), "policy", {}, result["version"])
    with pytest.raises(AssistantError):
        admin.patch_config(request, "policy", {"worker_timeout_seconds": -1}, result["version"])


def test_durable_scoped_grant_and_revocation_at_send(services):
    _, repo, authority, admin = services
    request = ctx(authority, admin=True)
    admin.classify(request, resource="memories/private", expected=None, classification="personal", owners=(OWNER.key,))
    target = ctx(authority, OTHER)
    assert not admin.access.can_read(target, "memories/private")
    grant = admin.authorize(request, grantee=OTHER.key, owner=OWNER.key, resources=["memories/private"])
    lease = admin.access.lease(target, ["memories/private"])
    admin.revoke(request, grant["id"])
    with pytest.raises(AssistantError):
        admin.access.revalidate(lease)
    with pytest.raises(AssistantError):
        admin.authorize(request, grantee=OTHER.key, owner=OTHER.key, resources=["memories/private"])


def test_restore_revokes_sessions_grants_and_role_run(services):
    store, repo, authority, admin = services
    request = ctx(authority, admin=True)
    admin.classify(request, resource="p", expected=None, classification="personal", owners=(OWNER.key,))
    admin.authorize(request, grantee=OTHER.key, owner=OWNER.key, resources=["p"])
    with store.db() as db:
        db.execute("INSERT INTO governance_sessions VALUES(?,?,?,?,?,?)", ("s", OWNER.key, "admin", 2000, repo.epoch(), 0))
    store.set_state("core-run", {"status": "active"})
    assert repo.session_active("s")
    repo.reset_after_restore()
    assert not repo.session_active("s") and not repo.grants(OTHER.key)
    assert store.state("core-run")["status"] == "paused"
    with pytest.raises(AssistantError):
        admin.read_config(request, "policy")


def test_source_change_invalidates_new_retrieval_of_old_summary(services):
    _, repo, authority, admin = services
    request = ctx(authority, admin=True)
    source = admin.classify(request, resource="source", expected=None, classification="public")
    admin.classify(request, resource="summary", expected=None, classification="public", sources=("source",))
    assert admin.access.can_read(ctx(authority), "summary")
    admin.classify(request, resource="source", expected=source["version"], classification="public")
    assert not admin.access.can_read(ctx(authority), "summary")


def test_legacy_store_declines_aggregate_snapshot_without_partial_metadata(services):
    from companion_core.provenance import freeze

    store, repo, authority, admin = services
    repo.set_policy(RecordPolicy("relationships/legacy", "v1", "personal", frozenset((OWNER.key,))), None, "test")
    lease = admin.access.lease(ctx(authority), ["relationships/legacy"])
    with pytest.raises(AssistantError) as exc:
        freeze(admin.access, lease)
    assert exc.value.code == "provenance_backend"
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM governance_provenance").fetchone()[0] == 0


def test_memory_is_filtered_before_ranking_and_hides_metadata(services):
    store, repo, authority, admin = services
    for item_id, owner, content in (("own", OWNER, "我的植物养护记忆"), ("hidden", OTHER, "秘密植物养护地址")):
        store.write("memories", {"schema_version": 1, "id": item_id, "kind": "experience", "content": content,
                                "source": {"type": "interaction", "recorded_at": "2026-10-23T00:00:00+00:00",
                                           "reference": "test"}, "confidence": "observed", "tags": [], "locked": False},
                    None, "test", item_id)
        repo.set_policy(RecordPolicy("memories/" + item_id, "v1", "personal", frozenset((owner.key,))), None, "test")
    context, guard = ContextService(store, admin.access).memory(ctx(authority), "植物养护")
    assert [r["data"]["id"] for r in context["records"]] == ["own"]
    assert context["total_records"] == 1
    assert "foundation_status" not in context and "relationship_scope" not in context
    guard()
    repo.set_policy(replace(repo.policy("memories/own"), version="v2"), "v1", "test")
    with pytest.raises(AssistantError):
        guard()
