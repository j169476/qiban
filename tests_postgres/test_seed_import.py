import json
import secrets

import pytest

from companion_core.access import RecordPolicy
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import AdminService, ContextService, GovernanceRepository
from companion_core.legacy_import import apply, capture
from companion_core.seed_import import select
from companion_core.sessions import SessionService
from companion_core.storage import Store


def legacy_source(path):
    legacy = Store(path)
    legacy.initialize()
    for identifier, source in (("foundation", "user_instruction"), ("development", "interaction")):
        legacy.write("memories", {"schema_version": 1, "id": identifier, "kind": "user_preference",
            "content": identifier + " private marker", "source": {"type": source,
            "recorded_at": "2026-10-23T00:00:00Z", "reference": "synthetic user instruction"},
            "confidence": "observed", "tags": [], "locked": True}, None, "test", identifier)
    card = legacy.read("persona")
    legacy.write("persona", {**card["data"], "foundation_memories": ["foundation"]}, card["version"], "test")
    legacy.set_state("core-model:2026-10-23", 17)
    legacy.set_state("native:synthetic:2026-10-23:web", 2)
    legacy.set_state("context:development-video", {"secret": "discarded development context"})
    legacy.set_state("run", {"status": "paused", "owner": "old"})
    with legacy.db() as db:
        db.execute("INSERT INTO sessions VALUES(?,?)", ("existing-admin", "admin"))
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                   ("development-event", "synthetic", "other", "dm_reply", "2026-10-23", "{}"))
        db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("old-action", "old-dedupe", "old", "g", "synthetic", "reply", "{}", "succeeded",
                    "2026-10-23", 1, None, "2026-10-23", "2026-10-23", "{}"))
    return legacy


def import_seed(pg_store, tmp_path):
    legacy = legacy_source(tmp_path / "legacy")
    original = capture(legacy.root, legacy.profile)
    snapshot = select(original, legacy.profile)
    with pg_store.db() as db:
        for table in ("record_owners", "source_dependencies", "governance_policies", "content_records", "state"):
            db.execute("DELETE FROM " + table)
    result = apply(pg_store, snapshot)
    return legacy, original, snapshot, result


def test_seed_import_keeps_only_foundations_and_operational_receipts(pg_store, tmp_path):
    legacy, original, snapshot, result = import_seed(pg_store, tmp_path)
    assert result["counts"]["memories"] == 1
    assert pg_store.read("persona") == legacy.read("persona")
    assert pg_store.read("memories", "foundation") == legacy.read("memories", "foundation")
    assert not pg_store.exists("memories", "development")
    assert pg_store.read("growth")["data"] == legacy.profile.defaults["growth"]
    assert pg_store.state("context:development-video") is None
    assert pg_store.state("core-model:2026-10-23") == 17
    assert pg_store.state("native:synthetic:2026-10-23:web") == 2
    assert pg_store.state("run")["status"] == "paused"
    with pg_store.db() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT dedupe FROM actions").fetchone()[0] == "old-dedupe"
        assert db.execute("SELECT mode FROM sessions").fetchone()[0] == "admin"
    assert len(original["tables"]["events"]["rows"]) == 1
    assert capture(legacy.root, legacy.profile)["fingerprint"] == original["fingerprint"]
    assert select(original, legacy.profile)["fingerprint"] == snapshot["fingerprint"]
    assert snapshot["fingerprint"] != original["fingerprint"]
    assert apply(pg_store, snapshot)["reused"]
    with pytest.raises(AssistantError, match="另一份"):
        apply(pg_store, original)


def test_first_password_claims_exact_private_seeds_with_live_revocation(pg_store, tmp_path):
    import_seed(pg_store, tmp_path)
    admin = AdminService(pg_store)
    sessions = SessionService(admin.repository, admin.authority)
    assert admin.repository.policy("memories/foundation").classification == "unclassified"
    sessions.configure(sessions.setup_ticket(), "synthetic-seed-password")
    role = sessions.request(sessions.login("synthetic-seed-password", "role"), "role")
    assert admin.access.can_read(role, "persona/@config")
    assert admin.access.can_read(role, "memories/foundation")
    context, check = ContextService(pg_store, admin.access).memory(role, "foundation")
    assert "foundation" in {r["data"]["id"] for r in context["records"]}
    check()
    stranger = ActorRef("bilibili", "synthetic-unverified-owner")
    other = admin.authority.issue(actor=stranger, conversation="private", audience="private",
                                  recipients=(stranger,), purpose="reply")
    public = admin.authority.issue(actor=None, conversation="public", audience="public", purpose="exploration")
    assert not admin.access.can_read(other, "memories/foundation")
    assert not admin.access.can_read(public, "persona/@config")
    with pg_store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_grants").fetchone()[0] == 0
    original = admin.repository.policy("memories/foundation")
    admin.repository.set_policy(RecordPolicy(original.resource, secrets.token_hex(16)), original.version, "test-admin")
    with pytest.raises(AssistantError):
        check()
    assert not admin.access.can_read(role, "memories/foundation")
    assert pg_store.state("run")["status"] == "paused"


@pytest.mark.parametrize("changed", ["content", "policy"])
def test_seed_setup_rechecks_pins_and_rolls_back_owner_creation(pg_store, tmp_path, changed):
    import_seed(pg_store, tmp_path)
    admin = AdminService(pg_store)
    sessions = SessionService(admin.repository, admin.authority)
    ticket = sessions.setup_ticket()
    if changed == "content":
        record = pg_store.read("memories", "foundation")
        pg_store.write("memories", {**record["data"], "content": "changed"}, record["version"],
                       "test-admin", "foundation", allow_locked=True)
    else:
        old = admin.repository.policy("memories/foundation")
        admin.repository.set_policy(RecordPolicy(old.resource, secrets.token_hex(16)), old.version, "test-admin")
    with pytest.raises(AssistantError, match="初始化前已变化"):
        sessions.configure(ticket, "synthetic-seed-password")
    assert pg_store.state("web-owner") is None
    assert pg_store.state("web-setup")["consumed"] is False
    assert GovernanceRepository(pg_store).policy("persona/@config").classification == "unclassified"


def test_seed_selection_rejects_missing_or_non_instruction_memory(tmp_path):
    legacy = legacy_source(tmp_path / "legacy")
    snapshot = capture(legacy.root, legacy.profile)
    original = json.dumps(snapshot["tables"], sort_keys=True)
    memory = next(r for r in snapshot["records"] if r["id"] == "foundation")
    memory["data"]["source"]["type"] = "interaction"
    with pytest.raises(AssistantError) as error:
        select(snapshot, legacy.profile)
    assert error.value.code == "seed_source"
    snapshot["records"].remove(memory)
    with pytest.raises(AssistantError) as error:
        select(snapshot, legacy.profile)
    assert error.value.code == "seed_missing"
    assert json.dumps(snapshot["tables"], sort_keys=True) == original
