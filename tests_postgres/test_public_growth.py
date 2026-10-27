from dataclasses import replace

import pytest

from companion_core.errors import AssistantError
from companion_core.public_workspace import PublicWorkspace


def seed(store):
    workspace = PublicWorkspace(store)
    relationship = {"schema_version": 1, "id": "public-person", "display_name": "Public person",
                    "affinity": 50, "interactions": 0, "events": [], "locked": False}
    workspace.write("relationships", relationship, None, "fixture", relationship["id"])
    workspace.read("relationships", relationship["id"])
    growth = workspace.read("growth")
    workspace.before_model({"growth": growth["data"]})
    workspace.write("growth", growth["data"], growth["version"], "fixture")
    return workspace


def test_routine_public_relationship_update_keeps_growth_readable(pg_store):
    seed(pg_store)
    # Recovery/maintenance can write before the next model lease is established.
    workspace = PublicWorkspace(pg_store)
    relationship = workspace.read("relationships", "public-person")
    workspace.before_model({"relationship": relationship["data"]})
    workspace.write("relationships", {**relationship["data"], "affinity": 51},
                    relationship["version"], "fixture", "public-person")
    workspace.check()
    assert PublicWorkspace(pg_store).read("growth")["data"]


def test_manual_public_relationship_correction_still_revokes_growth(pg_store):
    seed(pg_store)
    relationship = pg_store.read("relationships", "public-person")
    pg_store.write("relationships", {**relationship["data"], "affinity": 51},
                   relationship["version"], "administrator", "public-person")
    with pytest.raises(AssistantError):
        PublicWorkspace(pg_store).read("growth")


def test_failed_growth_commit_rolls_back_payload_and_policy(pg_store, monkeypatch):
    workspace = seed(pg_store)
    before = pg_store.state("public-growth")
    policy = workspace.repository.policy("partition/public-growth")
    workspace.before_model({"growth": workspace.read("growth")["data"]})

    def fail(*args):
        raise AssistantError("context_changed", "synthetic conflict")

    monkeypatch.setattr(workspace, "_classify", fail)
    with pytest.raises(AssistantError):
        workspace.write("growth", {**before["data"], "traits": ["synthetic"]}, before["version"], "fixture")
    assert pg_store.state("public-growth") == before
    assert workspace.repository.policy("partition/public-growth") == policy


@pytest.mark.parametrize("cause", ["missing_policy", "source_changed", "source_revoked"])
def test_growth_diagnostics_do_not_include_source_contents(pg_store, cause):
    workspace = seed(pg_store)
    repo = workspace.repository
    if cause == "missing_policy":
        with pg_store.db() as db:
            db.execute("DELETE FROM governance_policies WHERE resource='partition/public-growth'")
    else:
        policy = repo.policy("relationships/public-person")
        repo.set_policy(replace(policy, version="changed", valid=cause != "source_revoked"),
                        policy.version, "administrator")
    with pytest.raises(AssistantError) as error:
        PublicWorkspace(pg_store).read("growth")
    assert error.value.code.startswith("public_growth_")
    assert "public-person" not in str(error.value)


def test_explicit_rebuild_archives_invalid_state_and_keeps_revoked_notes_unreadable(pg_store):
    from companion_core.public_growth import preview, rebuild

    seed(pg_store)
    original = pg_store.state("public-growth")
    relationship = pg_store.read("relationships", "public-person")
    pg_store.write("relationships", {**relationship["data"], "affinity": 40},
                   relationship["version"], "administrator", "public-person")
    proposal = preview(pg_store)
    assert proposal["reason"].startswith("public_growth_")
    assert pg_store.state("public-growth") == original
    result = rebuild(pg_store, proposal["expected"], "administrator")
    assert pg_store.state(result["archive"])["original"] == original
    assert PublicWorkspace(pg_store).read("growth")["data"] == pg_store.profile.defaults["growth"]
    assert result["requires_explicit_start"]


def test_rebuild_rejects_changed_source_and_transaction_rolls_back(pg_store, monkeypatch):
    from companion_core.public_growth import preview, rebuild

    workspace = seed(pg_store)
    proposal = preview(pg_store)
    policy = workspace.repository.policy("relationships/public-person")
    workspace.repository.set_policy(replace(policy, version="changed"), policy.version, "administrator")
    with pytest.raises(AssistantError, match="预览后变化"):
        rebuild(pg_store, proposal["expected"], "administrator")
    proposal = preview(pg_store)
    original = pg_store.state("public-growth")
    from companion_core.governance import GovernanceRepository
    old = GovernanceRepository.audit

    def fail(self, actor, operation, *args, **kwargs):
        if operation == "public_growth_rebuild":
            raise RuntimeError("synthetic crash")
        return old(self, actor, operation, *args, **kwargs)

    monkeypatch.setattr(GovernanceRepository, "audit", fail)
    with pytest.raises(RuntimeError):
        rebuild(pg_store, proposal["expected"], "administrator")
    assert pg_store.state("public-growth") == original
    with pg_store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'public-growth-recovery:%'").fetchone()[0] == 0
