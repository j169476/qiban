"""Record sharing (SHARE-167) against a disposable PostgreSQL schema.

Every record, account, policy and source below is synthetic. No connector,
platform process or model runs, and no real database, credential or chat is read.
"""
import secrets
import time
import uuid
from dataclasses import replace

import pytest

from companion_core.access import AccessGrant, GraphLimits, RecordPolicy
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.record_sharing import GRANT_ROWS, KINDS, MAX_PREVIEWS
from companion_core.relations import RelationService
from qiban.desktop_management import DesktopManagement, provision_owner
from tests_postgres.test_desktop_reply_quota import call, denied, result

INSTALLATION = "installation-sharing"
SID = "S-1-5-21-sharing-owner"
BOB = ActorRef("qq", "10002", "qq-bot")
CAROL = ActorRef("wechat", "wxid_synthetic", "wechat-host")
DAVE = ActorRef("qq", "10003", "qq-bot")


def service_for(store):
    provision_owner(store, INSTALLATION, SID)
    return DesktopManagement(store, INSTALLATION, SID)


def register(service, *actors):
    relations = RelationService(service.repository, service.authority)
    for actor in actors:
        relations.register_actor(actor)


def memory(service, identifier="shared-memory", *, content="合成的共享资料", **extra):
    value = {"schema_version": 1, "id": identifier, "kind": "user_preference", "content": content,
             "confidence": "observed", "tags": [], "locked": False,
             "source": {"type": "user_instruction", "reference": "synthetic-sharing",
                        "recorded_at": "2026-11-08T00:00:00Z"}, **extra}
    created = call(service, "records.correct",
                   {"kind": "memories", "identifier": identifier, "expected": None, "value": value})
    assert created["ok"], created
    return created["result"]["version"]


def other(service, kind, identifier, *, content="合成的共享知识"):
    value = {"schema_version": 1, "id": identifier, "content": content,
             "tags": [], "locked": False,
             "source": {"type": "user_instruction", "reference": "synthetic-sharing",
                        "recorded_at": "2026-11-08T00:00:00Z"}}
    created = call(service, "records.correct",
                   {"kind": kind, "identifier": identifier, "expected": None, "value": value})
    assert created["ok"], created
    return created["result"]["version"]


def rewrite(service, identifier, *, content):
    current = service.store.read("memories", identifier)
    updated = call(service, "records.correct",
                   {"kind": "memories", "identifier": identifier, "expected": current["version"],
                    "value": {**current["data"], "content": content}})
    assert updated["ok"], updated
    return updated["result"]["version"]


def set_policy(service, resource, *, classification="personal", owners=None, valid=True,
               expires_at=None, sources=(), pins=(), conversation="", audience="private"):
    """Write one real policy row with the exact sources and pins requested."""
    current = service.repository.policy(resource)
    policy = RecordPolicy(resource, secrets.token_hex(16), classification,
                          frozenset({service.actor.key} if owners is None else owners),
                          conversation, audience, tuple(sources), valid, expires_at, tuple(pins))
    service.repository.set_policy(policy, current.version if current else None, "synthetic")
    return policy


def source_row(service, resource, *, version="v1"):
    policy = RecordPolicy(resource, version, "personal", frozenset({service.actor.key}), "", "private")
    service.repository.set_policy(policy, service.repository.policy(resource).version
                                  if service.repository.policy(resource) else None, "synthetic")
    return policy


def request_for(service, actor, *, audience="private", purpose="reply", conversation="thread-private"):
    return service.authority.issue(actor=actor, conversation=conversation, audience=audience,
                                   recipients=(actor,) if audience == "private" else (),
                                   purpose=purpose, lifetime=60)


def can_read(service, actor, resource, **kwargs):
    return service.admin.access.can_read(request_for(service, actor, **kwargs), resource)


def public_request(service, actor):
    return service.authority.issue(actor=actor, conversation="thread-public", audience="public",
                                   purpose="reply", lifetime=60)


def grant(service, kind, identifier, grantee, *, purposes=("reply",), days=30, owner=None,
          request_id=None):
    detail = result(service, "sharing.record", {"kind": kind, "identifier": identifier})
    chosen = owner if owner is not None else (detail["owner_options"][0] if detail["owner_options"] else "")
    preview = result(service, "sharing.preview_grant",
                     {"kind": kind, "identifier": identifier, "grantee": grantee.key,
                      "owner": chosen, "purposes": list(purposes), "days": days})
    return preview, result(service, "sharing.commit", {"token": preview["token"]}, request_id)


def grants_of(store, resource):
    with store.db() as db:
        rows = db.execute("SELECT g.id,g.grantee,g.revoked FROM grant_resources gr "
                          "JOIN governance_grants g ON g.id=gr.grant_id WHERE gr.resource=? "
                          "ORDER BY g.id", (resource,)).fetchall()
    return [dict(row) for row in rows]


def grant_row(service, grantee, resources, *, owner=None, purposes=("reply",), lifetime=3600,
              revoked=False, epoch=None):
    """One grant row written the way the core writes it, never through a preview."""
    resources = tuple(resources)
    policy = service.repository.policy(resources[0])
    grant = AccessGrant(secrets.token_hex(16), grantee.key,
                        owner or sorted(policy.owners)[0], frozenset(resources), frozenset(purposes),
                        "owner_admin", "synthetic-test", service.repository.clock() + lifetime,
                        service.repository.epoch() if epoch is None else epoch, revoked)
    service.repository.put_grant(grant, "synthetic")
    return grant


def audit_count(store, operation):
    with store.db() as db:
        return db.execute("SELECT COUNT(*) FROM governance_audit WHERE operation=?",
                          (operation,)).fetchone()[0]


def state_keys(store, prefix=""):
    with store.db() as db:
        return [row[0] for row in db.execute(
            "SELECT key FROM state WHERE key LIKE ? ORDER BY key", (prefix + "%",)).fetchall()]


def snapshot(store):
    with store.db() as db:
        content = [tuple(row) for row in db.execute(
            "SELECT kind,id,version FROM content_records ORDER BY kind,id").fetchall()]
        policies = [tuple(row) for row in db.execute(
            "SELECT resource,version,classification,owners,sources,source_versions,valid,expires_at "
            "FROM governance_policies ORDER BY resource").fetchall()]
        grants = [tuple(row) for row in db.execute(
            "SELECT id,grantee,revoked FROM governance_grants ORDER BY id").fetchall()]
        states = [row[0] for row in db.execute("SELECT key FROM state ORDER BY key").fetchall()]
    return {"content": content, "policies": policies, "grants": grants,
            "states": [key for key in states if not key.startswith("desktop-request:")]}


# --- one account's private reply only ---------------------------------------


def test_grant_only_widens_the_registered_accounts_private_reply(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB, CAROL)
    memory(service, "m-basic")
    resource = "memories/m-basic"
    set_policy(service, resource, classification="personal", owners={service.actor.key})
    assert not can_read(service, BOB, resource)
    before = snapshot(store)

    preview, committed = grant(service, "memories", "m-basic", BOB)
    assert preview["action"] == "grant" and preview["written"] is False
    assert preview["will_apply"] is True and preview["blockers"] == []
    assert preview["before"]["grants"] == 0 and preview["after"]["grants"] == 1
    assert committed["effective"] is True and committed["changed"] is True
    assert can_read(service, BOB, resource) is True

    # A private-chat grant is not a visibility change: group and public stay closed.
    assert can_read(service, BOB, resource, audience="group") is False
    assert can_read(service, BOB, resource, audience="public") is False
    # No other account and no other purpose is included.
    assert can_read(service, CAROL, resource) is False
    assert can_read(service, BOB, resource, purpose="exploration") is False
    assert service.admin.access.can_read(public_request(service, BOB), resource) is False

    # The record, its policy and its content are untouched; only one grant appeared.
    after = snapshot(store)
    assert after["content"] == before["content"]
    assert after["policies"] == before["policies"]
    assert len(after["grants"]) == len(before["grants"]) + 1
    assert after["states"] == before["states"]
    assert service.store.read("memories", "m-basic")["data"]["content"] == "合成的共享资料"
    assert audit_count(store, "desktop_sharing_grant") == 1


def test_a_maintenance_grant_never_enables_reply_and_vice_versa(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-purposes")
    resource = "memories/m-purposes"
    set_policy(service, resource, classification="personal")
    grant(service, "memories", "m-purposes", BOB, purposes=("maintenance",))
    assert can_read(service, BOB, resource, purpose="maintenance") is True
    assert can_read(service, BOB, resource, purpose="reply") is False


def test_sharing_never_authorizes_a_conversation_or_starts_a_platform(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB)
    memory(service, "m-runtime")
    set_policy(service, "memories/m-runtime", classification="personal")
    before = snapshot(store)
    grant(service, "memories", "m-runtime", BOB)
    after = snapshot(store)
    # Only the grant row (and its audit history) changes: no reply authorization,
    # reply quota, exclusion or running-platform state is created or moved.
    assert after["states"] == before["states"]
    assert after["content"] == before["content"]
    assert after["policies"] == before["policies"]
    for prefix in ("reply-authorization", "reply-policy", "reply-exclusion", "core-run:"):
        assert state_keys(store, prefix) == []


# --- refusals ---------------------------------------------------------------


def test_secret_unclassified_ownerless_and_unregistered_targets_are_refused(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-secret")
    set_policy(service, "memories/m-secret", classification="secret")
    memory(service, "m-unclassified")
    set_policy(service, "memories/m-unclassified", classification="unclassified")
    memory(service, "m-ownerless")
    set_policy(service, "memories/m-ownerless", owners=frozenset())
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-ownerless"})
    assert detail["owner_options"] == []

    base = {"kind": "memories", "identifier": "m-secret", "grantee": BOB.key,
            "owner": service.actor.key, "purposes": ["reply"], "days": 30}
    assert denied(service, "sharing.preview_grant", base) == "sharing_grant"
    assert denied(service, "sharing.preview_grant", {**base, "identifier": "m-unclassified"}) == "sharing_grant"
    assert denied(service, "sharing.preview_grant", {**base, "identifier": "m-ownerless"}) == "sharing_grant"
    # An unregistered account is not a target, however the request is formed.
    memory(service, "m-ok")
    set_policy(service, "memories/m-ok", classification="personal")
    assert denied(service, "sharing.preview_grant",
                  {**base, "identifier": "m-ok", "grantee": DAVE.key}) == "unknown_actor"
    # Forged values are refused before any token is issued.
    for bad in ({"purposes": ["management"]}, {"purposes": []}, {"purposes": ["reply", "reply"]},
                {"days": 0}, {"days": 366}, {"days": True}, {"days": "30"},
                {"owner": "someone-else"}, {"grantee": ""}):
        assert denied(service, "sharing.preview_grant", {**base, "identifier": "m-ok", **bad}) == "sharing_grant"
    # The core lifetime bound is not widened here.
    accepted = result(service, "sharing.preview_grant",
                      {**base, "identifier": "m-ok", "days": 365})
    assert accepted["token"] and denied(service, "sharing.preview_grant",
                                        {**base, "identifier": "m-ok", "days": 365 + 1}) == "sharing_grant"
    assert grants_of(service.store, "memories/m-ok") == []


def test_personal_facts_and_shared_baseline_material_are_refused(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    card = service.store.read("persona")
    foundation = "foundation-shared"
    assert call(service, "config.patch", {"kind": "persona",
                                          "changes": {"foundation_memories": [foundation]},
                                          "expected": card["version"]})["ok"]
    memory(service, foundation)
    set_policy(service, "memories/" + foundation, classification="public")
    fact = {"schema_version": 1, "id": "fact-1", "kind": "experience", "content": "用户住在合成城市",
            "confidence": "observed", "tags": [], "locked": False,
            "fact": {"actor": BOB.key, "account": "qq-bot", "conversation": "thread-private",
                     "attribute": "residence", "value": "合成城市", "quote": "我住在合成城市",
                     "valid_from": "2026-11-08T00:00:00+00:00", "supersedes": []},
            "source": {"type": "user_instruction", "reference": "synthetic-sharing",
                       "recorded_at": "2026-11-08T00:00:00Z"}}
    assert call(service, "records.correct",
                {"kind": "memories", "identifier": "fact-1", "expected": None, "value": fact})["error"] \
        == "fact_append_required"
    # Write the fact row directly, as the fact pipeline would.
    service.store.write("memories", fact, None, "synthetic-fact", "fact-1")
    set_policy(service, "memories/fact-1", classification="personal")

    for identifier in (foundation, "fact-1"):
        assert denied(service, "sharing.record", {"kind": "memories", "identifier": identifier}) == "sharing_record"
        assert denied(service, "sharing.preview_grant",
                      {"kind": "memories", "identifier": identifier, "grantee": BOB.key,
                       "owner": service.actor.key, "purposes": ["reply"], "days": 30}) == "sharing_record"
        assert denied(service, "sharing.preview_classify",
                      {"kind": "memories", "identifier": identifier,
                       "classification": "public"}) == "sharing_record"
    # The shared baseline and the fact history are unchanged by the refusals.
    assert service.store.read("memories", foundation)["data"]["content"] == "合成的共享资料"
    assert service.store.read("persona")["data"]["foundation_memories"] == [foundation]
    listed = result(service, "sharing.records", {"after": "", "limit": 50})
    assert {row["identifier"] for row in listed["records"]}.isdisjoint({foundation, "fact-1"})


def test_only_ordinary_record_kinds_are_shareable(pg_store):
    service = service_for(pg_store)
    memory(service, "m-visible")
    other(service, "knowledge", "k-visible")
    other(service, "diaries", "d-visible")
    listed = result(service, "sharing.records", {"after": "", "limit": 50})
    assert {(row["kind"], row["identifier"]) for row in listed["records"]} == {
        ("diaries", "d-visible"), ("knowledge", "k-visible"), ("memories", "m-visible")}
    assert listed["kinds"] == list(KINDS)
    for kind, identifier in (("persona", None), ("manifest", None), ("memories", None)):
        assert denied(service, "sharing.record", {"kind": kind, "identifier": identifier}) == "sharing_record"
    assert denied(service, "sharing.record", {"kind": "secrets", "identifier": "x"}) == "sharing_record"
    assert denied(service, "sharing.record", {"kind": "memories", "identifier": "../escape"}) == "sharing_record"


# --- classification keeps the exact stored permissions ----------------------


def test_classification_keeps_exact_source_pins_validity_and_expiry(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB)
    memory(service, "m-pinned")
    resource = "memories/m-pinned"
    source_row(service, "governance/source-a", version="v1")
    expiry = time.time() + 3600
    set_policy(service, resource, classification="personal", owners={service.actor.key, BOB.key},
               valid=True, expires_at=expiry, conversation="thread-private", audience="private",
               sources=("governance/source-a",), pins=(("governance/source-a", "v1"),))
    pinned = service.repository.policy(resource)
    # The source rotates after the record was pinned: a re-pin would launder the change.
    source_row(service, "governance/source-a", version="v2")
    assert not can_read(service, BOB, resource)

    preview = result(service, "sharing.preview_classify",
                     {"kind": "memories", "identifier": "m-pinned", "classification": "public"})
    assert preview["direction"] == "扩大"
    assert preview["will_apply"] is False
    assert any("来源" in line for line in preview["blockers"])
    committed = result(service, "sharing.commit", {"token": preview["token"]})
    assert committed["changed"] is True and committed["effective"] is False

    stored = service.repository.policy(resource)
    assert stored.classification == "public"
    assert stored.version != pinned.version
    # Everything else is byte-for-byte the stored permission.
    assert stored.owners == pinned.owners
    assert stored.sources == pinned.sources == ("governance/source-a",)
    assert stored.source_versions == pinned.source_versions == (("governance/source-a", "v1"),)
    assert stored.valid == pinned.valid is True
    assert stored.expires_at == pinned.expires_at == expiry
    assert (stored.conversation, stored.audience) == (pinned.conversation, pinned.audience)
    # A rotated source still denies the wider classification: sharing does not bypass it.
    assert can_read(service, BOB, resource) is False
    assert service.admin.access.can_read(public_request(service, BOB), resource) is False
    assert audit_count(store, "desktop_sharing_classify") == 1


@pytest.mark.parametrize("state", ["revoked", "expired", "secret", "unclassified", "pins_incomplete"])
def test_restricted_sources_are_reported_and_still_deny_after_sharing(pg_store, state):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-source")
    resource = "memories/m-source"
    now = time.time()
    sources, pins = (), ()
    if state == "pins_incomplete":
        # A real policy with a pin that no longer matches its declared sources.
        source_row(service, "governance/source-b", version="v1")
        sources, pins = ("governance/source-b",), ()
    else:
        source_row(service, "governance/source-b", version="v1")
        sources, pins = ("governance/source-b",), (("governance/source-b", "v1"),)
    set_policy(service, resource, classification="personal", sources=sources, pins=pins)
    if state == "revoked":
        service.repository.set_policy(replace(service.repository.policy("governance/source-b"),
                                              valid=False, version="v1b"),
                                      "v1", "synthetic")
    elif state == "expired":
        service.repository.set_policy(replace(service.repository.policy("governance/source-b"),
                                              expires_at=now - 1, version="v1b"),
                                      "v1", "synthetic")
    elif state in ("secret", "unclassified"):
        service.repository.set_policy(replace(service.repository.policy("governance/source-b"),
                                              classification=state, version="v1b"),
                                      "v1", "synthetic")

    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-source"})
    assert detail["source"]["ok"] is False and detail["source"]["total_issues"] >= 1
    assert detail["blockers"]

    preview = result(service, "sharing.preview_grant",
                     {"kind": "memories", "identifier": "m-source", "grantee": BOB.key,
                      "owner": service.actor.key, "purposes": ["reply"], "days": 30})
    assert preview["will_apply"] is False
    assert preview["blockers"]
    assert any("来源" in line or "版本" in line for line in preview["blockers"])
    committed = result(service, "sharing.commit", {"token": preview["token"]})
    assert committed["effective"] is False
    assert "仍不能生效" in committed["notice"]
    # The grant exists, but the unchanged source still denies every request.
    assert len(grants_of(service.store, resource)) == 1
    assert can_read(service, BOB, resource) is False


@pytest.mark.parametrize("state", ["record_expired", "record_revoked"])
def test_an_expired_or_revoked_record_stays_unreadable_after_a_grant(pg_store, state):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-state")
    resource = "memories/m-state"
    expiry = (time.time() - 1) if state == "record_expired" else None
    set_policy(service, resource, classification="personal",
               valid=state != "record_revoked", expires_at=expiry)
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-state"})
    assert detail["blockers"]
    preview, committed = grant(service, "memories", "m-state", BOB)
    assert preview["will_apply"] is False and committed["effective"] is False
    assert can_read(service, BOB, resource) is False
    # Sharing never renewed validity or extended the expiry.
    stored = service.repository.policy(resource)
    assert stored.valid is (state != "record_revoked")
    assert stored.expires_at == expiry


# --- revocation -------------------------------------------------------------


def test_revoke_invalidates_a_frozen_lease_and_leaves_other_grants(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB, CAROL)
    memory(service, "m-two")
    resource = "memories/m-two"
    set_policy(service, resource, classification="personal")
    _, bob_grant = grant(service, "memories", "m-two", BOB)
    preview, carol_grant = grant(service, "memories", "m-two", CAROL)
    assert bob_grant["grant_id"] != carol_grant["grant_id"]

    # A lease frozen before the revocation is the exact thing a send re-checks.
    lease = service.admin.access.lease(request_for(service, BOB), [resource])
    service.admin.access.revalidate(lease)
    assert can_read(service, CAROL, resource) is True

    revoke = result(service, "sharing.preview_revoke",
                    {"kind": "memories", "identifier": "m-two", "grant_id": bob_grant["grant_id"]})
    assert revoke["action"] == "revoke" and revoke["before"]["grants"] == 2
    assert revoke["after"]["grants"] == 1
    assert revoke["effect"] and all(check["allowed"] is False for check in revoke["effect"])
    committed = result(service, "sharing.commit", {"token": revoke["token"]})
    assert committed["revoked"] is True and committed["changed"] is True

    with pytest.raises(AssistantError):
        service.admin.access.revalidate(lease)
    assert can_read(service, BOB, resource) is False
    # The other account keeps exactly the grant it had.
    assert can_read(service, CAROL, resource) is True
    remaining = grants_of(store, resource)
    assert [row["id"] for row in remaining if not row["revoked"]] == [carol_grant["grant_id"]]
    assert audit_count(store, "desktop_sharing_revoke") == 1


def test_revoke_refuses_a_foreign_grant_and_a_second_revocation(pg_store):
    service = service_for(pg_store)
    register(service, BOB, CAROL)
    memory(service, "m-first")
    memory(service, "m-second")
    set_policy(service, "memories/m-first", classification="personal")
    set_policy(service, "memories/m-second", classification="personal")
    _, first = grant(service, "memories", "m-first", BOB)
    _, second = grant(service, "memories", "m-second", BOB)

    # A grant of another record can never be revoked through this record.
    assert denied(service, "sharing.preview_revoke",
                  {"kind": "memories", "identifier": "m-first",
                   "grant_id": second["grant_id"]}) == "sharing_revoke"
    assert denied(service, "sharing.preview_revoke",
                  {"kind": "memories", "identifier": "m-first",
                   "grant_id": "not-this-grant"}) == "sharing_revoke"
    assert denied(service, "sharing.preview_revoke",
                  {"kind": "memories", "identifier": "m-first",
                   "grant_id": first["grant_id"] + "/../"}) == "sharing_revoke"
    preview = result(service, "sharing.preview_revoke",
                     {"kind": "memories", "identifier": "m-first", "grant_id": first["grant_id"]})
    assert result(service, "sharing.commit", {"token": preview["token"]})["revoked"] is True
    # Revoking the same grant again is reported, not silently repeated.
    assert denied(service, "sharing.preview_revoke",
                  {"kind": "memories", "identifier": "m-first",
                   "grant_id": first["grant_id"]}) == "sharing_revoke"
    assert can_read(service, BOB, "memories/m-second") is True
    assert [row["revoked"] for row in grants_of(service.store, "memories/m-second")] == [0]


# --- preview token lifetime and re-check ------------------------------------


@pytest.mark.parametrize("changed", ["token", "record", "policy", "source", "dependency", "grants",
                                     "session", "expiry", "lock", "restart"])
def test_a_preview_token_only_commits_what_was_previewed(pg_store, changed, monkeypatch):
    store = pg_store
    service = service_for(store)
    register(service, BOB, CAROL)
    memory(service, "m-token")
    resource = "memories/m-token"
    source_row(service, "governance/token-source", version="v1")
    set_policy(service, resource, classification="personal",
               sources=("governance/token-source",), pins=(("governance/token-source", "v1"),))
    preview = result(service, "sharing.preview_grant",
                     {"kind": "memories", "identifier": "m-token", "grantee": BOB.key,
                      "owner": service.actor.key, "purposes": ["reply"], "days": 30})
    token = preview["token"]
    if changed == "token":
        token = secrets.token_urlsafe(24)
    elif changed == "record":
        rewrite(service, "m-token", content="预览后被另一入口修改")
    elif changed == "policy":
        set_policy(service, resource, classification="personal", valid=True)
    elif changed == "source":
        source_row(service, "governance/token-source", version="v2")
    elif changed == "dependency":
        memory(service, "m-derived")
        set_policy(service, "memories/m-derived", classification="personal",
                   sources=(resource,), pins=((resource, service.repository.policy(resource).version),))
    elif changed == "grants":
        grant(service, "memories", "m-token", CAROL)
    elif changed == "session":
        service = DesktopManagement(store, INSTALLATION, SID)
    elif changed == "expiry":
        monkeypatch.setattr("companion_core.record_sharing.time.monotonic", lambda: 1e9)
    elif changed == "lock":
        service.enable_lock("Synthetic-Sharing-Password-2026")
        service.lock()
        service.unlock("Synthetic-Sharing-Password-2026")
    else:
        service = DesktopManagement(store, INSTALLATION, SID)
    reply = call(service, "sharing.commit", {"token": token})
    assert not reply["ok"], reply
    assert reply["error"] in ("edit_conflict", "sharing_preview_expired", "desktop_locked")
    # Nothing was granted by a stale or forged token; "grants" is the one branch that
    # deliberately created a grant before the commit was attempted.
    expected = 1 if changed == "grants" else 0
    assert len(grants_of(store, resource)) == expected


def test_commit_is_single_use_and_a_repeated_request_returns_the_first_receipt(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB)
    memory(service, "m-once")
    set_policy(service, "memories/m-once", classification="personal")
    preview = result(service, "sharing.preview_grant",
                     {"kind": "memories", "identifier": "m-once", "grantee": BOB.key,
                      "owner": service.actor.key, "purposes": ["reply"], "days": 30})
    key = uuid.uuid4().hex
    first = call(service, "sharing.commit", {"token": preview["token"]}, key)
    assert first["ok"], first
    reopened = DesktopManagement(store, INSTALLATION, SID)
    assert call(reopened, "sharing.commit", {"token": preview["token"]}, key) == first
    assert len(grants_of(store, "memories/m-once")) == 1
    assert audit_count(store, "desktop_sharing_grant") == 1
    # A consumed token cannot be committed a second time under a new request id.
    assert denied(reopened, "sharing.commit", {"token": preview["token"]},
                  uuid.uuid4().hex) == "sharing_preview_expired"
    assert len(grants_of(store, "memories/m-once")) == 1
    # Reusing a request id for different arguments is refused.
    second = result(reopened, "sharing.preview_classify",
                    {"kind": "memories", "identifier": "m-once", "classification": "public"})
    assert denied(reopened, "sharing.commit", {"token": second["token"]}, key) == "request_id_conflict"


def test_cancel_writes_nothing_and_drops_the_token(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB)
    memory(service, "m-cancel")
    set_policy(service, "memories/m-cancel", classification="personal")
    before = snapshot(store)
    preview = result(service, "sharing.preview_grant",
                     {"kind": "memories", "identifier": "m-cancel", "grantee": BOB.key,
                      "owner": service.actor.key, "purposes": ["reply"], "days": 30})
    cancelled = result(service, "sharing.cancel", {"token": preview["token"]})
    assert cancelled["cancelled"] is True and cancelled["written"] is False
    assert snapshot(store) == before
    assert denied(service, "sharing.commit", {"token": preview["token"]}) == "sharing_preview_expired"
    assert denied(service, "sharing.cancel", {"token": preview["token"]}) == "sharing_preview_expired"
    assert grants_of(store, "memories/m-cancel") == []


@pytest.mark.parametrize("action", ["grant", "classify"])
def test_a_failed_receipt_rolls_the_whole_commit_back(pg_store, action, monkeypatch):
    store = pg_store
    service = service_for(store)
    register(service, BOB)
    memory(service, "m-rollback")
    resource = "memories/m-rollback"
    set_policy(service, resource, classification="personal")
    before = snapshot(store)
    args = {"kind": "memories", "identifier": "m-rollback"}
    if action == "grant":
        args = {**args, "grantee": BOB.key, "owner": service.actor.key,
                "purposes": ["reply"], "days": 30}
        preview = result(service, "sharing.preview_grant", args)
    else:
        preview = result(service, "sharing.preview_classify", {**args, "classification": "public"})

    original = store.set_state

    def fail(key, value, db=None):
        if key.startswith("desktop-request:"):
            raise AssistantError("injected", "synthetic interruption")
        return original(key, value, db)

    monkeypatch.setattr(store, "set_state", fail)
    assert denied(service, "sharing.commit", {"token": preview["token"]}) == "injected"
    # The authorization and its request receipt are one unit of work: neither the
    # grant/classification nor the receipt survives a failed receipt write.
    assert snapshot(store) == before
    assert audit_count(store, "desktop_sharing_grant") == 0
    assert audit_count(store, "desktop_sharing_classify") == 0
    monkeypatch.setattr(store, "set_state", original)
    # The token was consumed before that outer failure, so a retry is refused and
    # the administrator previews again instead of silently reusing a stale scope.
    assert denied(service, "sharing.commit", {"token": preview["token"]}) == "sharing_preview_expired"
    if action == "grant":
        fresh = result(service, "sharing.preview_grant", args)
    else:
        fresh = result(service, "sharing.preview_classify", {**args, "classification": "public"})
    assert result(service, "sharing.commit", {"token": fresh["token"]})["changed"] is True
    assert len(grants_of(store, resource)) == (1 if action == "grant" else 0)
    assert audit_count(store, "desktop_sharing_grant") == (1 if action == "grant" else 0)


# --- bounded paging and real registration -----------------------------------


def test_listing_pages_are_bounded_and_the_cursor_is_validated(pg_store):
    service = service_for(pg_store)
    for index in range(7):
        memory(service, f"m-page-{index}")
    first = result(service, "sharing.records", {"after": "", "limit": 3})
    assert first["count"] == 3 and first["next"] == "memories/m-page-2"
    second = result(service, "sharing.records", {"after": first["next"], "limit": 3})
    assert second["count"] == 3 and second["next"] == "memories/m-page-5"
    third = result(service, "sharing.records", {"after": second["next"], "limit": 3})
    assert [row["identifier"] for row in third["records"]] == ["m-page-6"]
    assert third["next"] is None
    assert result(service, "sharing.records", {"after": "", "limit": 50})["count"] == 7
    # Every returned cursor is accepted and never repeats a row.
    seen, cursor = [], ""
    while True:
        page = result(service, "sharing.records", {"after": cursor, "limit": 2})
        seen.extend(row["cursor"] for row in page["records"])
        if not page["next"]:
            break
        cursor = page["next"]
    assert seen == [f"memories/m-page-{index}" for index in range(7)]
    for bad in ({"after": "memories/", "limit": 3}, {"after": "secrets/x", "limit": 3},
                {"after": "memories" + "/x" * 200, "limit": 3}, {"after": None, "limit": 3},
                {"after": "", "limit": 0}, {"after": "", "limit": 51}, {"after": "", "limit": True}):
        assert denied(service, "sharing.records", bad) == "sharing_query"
    assert denied(service, "sharing.records", {"after": "", "limit": 3, "administrator": True}) \
        == "management_operation"


def test_targets_are_paged_registered_accounts_only(pg_store):
    service = service_for(pg_store)
    actors = [ActorRef("qq", f"1000{index}", "qq-bot") for index in range(60)]
    register(service, *actors)
    first = result(service, "sharing.targets", {"after": ""})
    assert len(first["targets"]) == 50 and first["next"]
    second = result(service, "sharing.targets", {"after": first["next"]})
    combined = [row["key"] for row in first["targets"] + second["targets"]]
    assert len(combined) == 60 and len(set(combined)) == 60
    assert {row["key"] for row in first["targets"] + second["targets"]} == {actor.key for actor in actors}
    for row in first["targets"]:
        assert row["label"].startswith("QQ · ")
        assert set(row) >= {"key", "platform", "user_id", "scope", "label"}
    assert denied(service, "sharing.targets", {"after": None}) == "sharing_query"


def test_sharing_requires_an_unlocked_administrator(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB)
    memory(service, "m-auth")
    set_policy(service, "memories/m-auth", classification="personal")
    forged = service.authority.issue(actor=service.actor, conversation="desktop:forged",
                                     audience="private", recipients=(service.actor,),
                                     purpose="management", administrator=True,
                                     session_id=service.session_id)
    with pytest.raises(AssistantError) as error:
        service.sharing.listing(forged, "", 5)
    assert error.value.code == "access_denied"
    plain = service.authority.issue(actor=service.actor, conversation="desktop:" + service.instance,
                                    audience="private", recipients=(service.actor,),
                                    purpose="management", session_id=service.session_id)
    with pytest.raises(AssistantError) as error:
        service.sharing.detail(plain, "memories", "m-auth")
    assert error.value.code == "access_denied"

    service.enable_lock("Synthetic-Sharing-Password-2026")
    service.lock()
    assert denied(service, "sharing.records", {"after": "", "limit": 5}) == "desktop_locked"
    assert denied(service, "sharing.record", {"kind": "memories", "identifier": "m-auth"}) == "desktop_locked"
    assert denied(service, "sharing.targets", {"after": ""}) == "desktop_locked"
    assert denied(service, "sharing.grants",
                  {"kind": "memories", "identifier": "m-auth", "after": ""}) == "desktop_locked"
    assert denied(service, "sharing.commit", {"token": "x"}) == "desktop_locked"
    service.unlock("Synthetic-Sharing-Password-2026")
    assert result(service, "sharing.records", {"after": "", "limit": 5})["count"] == 1
    # Unknown operations and malformed arguments are refused by the registry.
    for op, args in (("sharing.grant", {}), ("sharing.commit", {}),
                     ("sharing.commit", {"token": 5}), ("sharing.cancel", {"token": ["x"]}),
                     ("sharing.preview_grant", {}), ("sharing.delete", {}),
                     ("sharing.record", {"kind": "memories", "identifier": "m-auth", "extra": 1})):
        assert denied(service, op, args) in ("management_operation", "sharing_preview_expired",
                                            "sharing_record")
    # A wrongly typed token is still a clean refusal, never a grant.
    assert denied(service, "sharing.commit", {"token": 5}) == "sharing_preview_expired"
    assert denied(service, "sharing.cancel", {"token": ["x"]}) == "sharing_preview_expired"


def test_reading_sharing_never_writes(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB)
    memory(service, "m-read")
    set_policy(service, "memories/m-read", classification="personal")
    grant(service, "memories", "m-read", BOB)
    before = snapshot(store)
    with store.db() as db:
        policy_audit = db.execute("SELECT COUNT(*) FROM governance_audit WHERE operation='policy'").fetchone()[0]
    for op, args in (("sharing.records", {"after": "", "limit": 5}),
                     ("sharing.record", {"kind": "memories", "identifier": "m-read"}),
                     ("sharing.targets", {"after": ""}),
                     ("sharing.grants", {"kind": "memories", "identifier": "m-read", "after": ""}),
                     ("sharing.preview_grant", {"kind": "memories", "identifier": "m-read",
                                                "grantee": BOB.key, "owner": service.actor.key,
                                                "purposes": ["reply"], "days": 30}),
                     ("sharing.preview_classify", {"kind": "memories", "identifier": "m-read",
                                                    "classification": "restricted"})):
        result(service, op, args)
    assert snapshot(store) == before
    # A preview only lives in memory: no receipt row and no audit row is written.
    assert audit_count(store, "desktop_sharing_grant") == 1
    with store.db() as db:
        now = db.execute("SELECT COUNT(*) FROM governance_audit WHERE operation='policy'").fetchone()[0]
    assert now == policy_audit


# --- bounded paging past excluded records -----------------------------------


def fact_row(service, identifier, *, value="合成城市"):
    """A personal fact, written directly as the fact pipeline would."""
    fact = {"schema_version": 1, "id": identifier, "kind": "experience",
            "content": "用户住在" + value, "confidence": "observed", "tags": [], "locked": False,
            "fact": {"actor": BOB.key, "account": "qq-bot", "conversation": "thread-private",
                     "attribute": "residence", "value": value, "quote": "我住在" + value,
                     "valid_from": "2026-11-08T00:00:00+00:00", "supersedes": []},
            "source": {"type": "user_instruction", "reference": "synthetic-sharing",
                       "recorded_at": "2026-11-08T00:00:00Z"}}
    service.store.write("memories", fact, None, "synthetic-fact", identifier)
    set_policy(service, "memories/" + identifier, classification="personal")
    return "memories/" + identifier


def test_a_long_run_of_excluded_records_still_pages_forward(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    for index in range(70):
        fact_row(service, "fact-%02d" % index)
    memory(service, "m-visible")
    set_policy(service, "memories/m-visible", classification="personal")

    first = result(service, "sharing.records", {"after": "", "limit": 20})
    # Every row on this page is an excluded personal fact: the page is empty but
    # it still advances, so the ordinary record behind them is reachable.
    assert first["records"] == [] and first["count"] == 0
    assert first["next"] == "memories/fact-62"

    seen, cursor, pages = [], first["next"], 1
    while cursor:
        page = result(service, "sharing.records", {"after": cursor, "limit": 20})
        pages += 1
        assert pages < 10
        seen.extend(row["cursor"] for row in page["records"])
        assert page["next"] != cursor
        cursor = page["next"]
    assert seen == ["memories/m-visible"]
    assert all("fact" not in row for row in seen)
    # The same exclusion holds for the whole listing.
    everything = result(service, "sharing.records", {"after": "", "limit": 50})
    assert [row["identifier"] for row in everything["records"]] == ["m-visible"]
    assert everything["next"] is None


# --- owners that are not external grantees ----------------------------------


def test_a_local_owner_can_be_classified_but_is_never_a_grant_target(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-local-owner")
    resource = "memories/m-local-owner"
    set_policy(service, resource, classification="personal", owners={service.actor.key})
    assert service.actor.platform == "local"
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-local-owner"})
    assert detail["owner_options"] == [service.actor.key]
    # A local-room owner is not an eligible external grantee, so it is neither a
    # target nor usable as one, however the request is formed.
    assert denied(service, "sharing.preview_grant",
                  {"kind": "memories", "identifier": "m-local-owner", "grantee": service.actor.key,
                   "owner": service.actor.key, "purposes": ["reply"], "days": 30}) == "unknown_actor"
    # Classification is still manageable and the effect check uses the real owner
    # identity instead of reporting "no account".
    preview = result(service, "sharing.preview_classify",
                     {"kind": "memories", "identifier": "m-local-owner", "classification": "restricted"})
    assert preview["effect"] and all(check["allowed"] is False for check in preview["effect"])
    assert preview["will_apply"] is False
    assert preview["effect_actor"] and preview["effect_actor"].startswith("本机 · ")
    assert preview["direction"] == "收窄"
    assert any("私聊" in line for line in preview["direction_detail"]["narrowed"])
    committed = result(service, "sharing.commit", {"token": preview["token"]})
    assert committed["changed"] is True
    stored = service.repository.policy(resource)
    assert stored.classification == "restricted"
    assert stored.owners == frozenset({service.actor.key})


def test_an_ownerless_record_is_managed_without_inventing_an_identity(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-no-owner")
    resource = "memories/m-no-owner"
    set_policy(service, resource, classification="personal", owners=frozenset())
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-no-owner"})
    assert detail["owner_options"] == [] and detail["policy"]["owners"] == []

    preview = result(service, "sharing.preview_classify",
                     {"kind": "memories", "identifier": "m-no-owner", "classification": "public"})
    assert preview["effect_actor"] is None and preview["owner_checked"] is None
    # Only the identity-free public shape is checked, and no account is invented.
    assert {check["shape"] for check in preview["effect"]} == {"public_read"}
    assert preview["will_apply"] is True and preview["direction"] == "扩大"
    assert "没有登记归属账号" in preview["effect_note"]
    committed = result(service, "sharing.commit", {"token": preview["token"]})
    assert committed["changed"] is True and committed["effective"] is True
    assert service.repository.policy(resource).owners == frozenset()
    # Without an owner there is no cross-account grant to preview either.
    assert denied(service, "sharing.preview_grant",
                  {"kind": "memories", "identifier": "m-no-owner", "grantee": BOB.key,
                   "owner": service.actor.key, "purposes": ["reply"], "days": 30}) == "sharing_grant"


# --- classification is not a total order ------------------------------------


def test_conversation_and_personal_change_audiences_in_both_directions(pg_store):
    service = service_for(pg_store)
    memory(service, "m-conv")
    resource = "memories/m-conv"
    set_policy(service, resource, classification="conversation", owners={service.actor.key},
               conversation="thread-group", audience="group")
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-conv"})
    assert detail["policy"]["audience"] == "group"

    # Group conversation -> owner private chat adds private-chat access and removes
    # the original group audience: neither "wider" nor "narrower" alone.
    preview = result(service, "sharing.preview_classify",
                     {"kind": "memories", "identifier": "m-conv", "classification": "personal"})
    assert preview["direction"] == "同时扩大与收窄"
    assert any("私聊" in line for line in preview["direction_detail"]["widened"])
    assert any("原会话" in line for line in preview["direction_detail"]["narrowed"])
    assert preview["direction_detail"]["basis"]
    shapes = {check["shape"]: check for check in preview["effect"]}
    assert shapes["private_reply"]["allowed"] is True
    assert shapes["conversation"]["allowed"] is False
    assert result(service, "sharing.commit", {"token": preview["token"]})["changed"] is True

    # And back again: the same pair of audiences moves in the opposite direction.
    back = result(service, "sharing.preview_classify",
                  {"kind": "memories", "identifier": "m-conv", "classification": "conversation"})
    assert back["direction"] == "同时扩大与收窄"
    assert any("原会话" in line for line in back["direction_detail"]["widened"])
    assert any("私聊" in line for line in back["direction_detail"]["narrowed"])

    # A genuinely one-way change keeps a one-way label.
    memory(service, "m-pub")
    set_policy(service, "memories/m-pub", classification="public")
    narrowing = result(service, "sharing.preview_classify",
                       {"kind": "memories", "identifier": "m-pub", "classification": "personal"})
    assert narrowing["direction"] == "收窄"
    assert narrowing["direction_detail"]["widened"] == []
    assert any("公开" in line for line in narrowing["direction_detail"]["narrowed"])


# --- effect checks cover the selected purposes and original audience ---------


def test_effect_checks_cover_the_selected_purposes_and_the_original_audience(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-maint")
    set_policy(service, "memories/m-maint", classification="personal")
    preview = result(service, "sharing.preview_grant",
                     {"kind": "memories", "identifier": "m-maint", "grantee": BOB.key,
                      "owner": service.actor.key, "purposes": ["maintenance"], "days": 30})
    # A maintenance-only grant is checked for maintenance only: no invented reply
    # failure, and the written scope and the readable scope are reported apart.
    assert {check["purpose"] for check in preview["effect"]} == {"maintenance"}
    assert all("回复" not in check["label"] for check in preview["effect"])
    assert preview["configured_purposes"] == ["maintenance"]
    assert preview["will_apply"] is True and preview["blockers"] == []
    committed = result(service, "sharing.commit", {"token": preview["token"]})
    assert committed["effective"] is True and committed["purposes"] == ["maintenance"]
    assert can_read(service, BOB, "memories/m-maint", purpose="maintenance") is True
    assert can_read(service, BOB, "memories/m-maint", purpose="reply") is False

    memory(service, "m-group")
    resource = "memories/m-group"
    set_policy(service, resource, classification="conversation", owners={service.actor.key},
               conversation="thread-group", audience="group")
    preview = result(service, "sharing.preview_grant",
                     {"kind": "memories", "identifier": "m-group", "grantee": BOB.key,
                      "owner": service.actor.key, "purposes": ["reply"], "days": 30})
    shapes = {check["shape"]: check for check in preview["effect"]}
    # The original group conversation is its own shape and stays reported; the
    # private grant neither widens it nor silences it.
    assert shapes["conversation"]["audience"] == "group"
    assert shapes["conversation"]["allowed"] is True
    assert shapes["private_reply"]["allowed"] is False
    assert preview["will_apply"] is False
    committed = result(service, "sharing.commit", {"token": preview["token"]})
    assert committed["changed"] is True and committed["effective"] is False
    assert "仍不能生效" in committed["notice"]
    assert can_read(service, BOB, resource) is False


# --- revocation covers the whole grant --------------------------------------


def test_revocation_shows_and_rechecks_every_covered_record(pg_store):
    service = service_for(pg_store)
    register(service, BOB, CAROL)
    resources = []
    for index in range(101):
        identifier = "m-wide-%03d" % index
        memory(service, identifier)
        set_policy(service, "memories/" + identifier, classification="personal")
        resources.append("memories/" + identifier)
    covered = resources[:100]
    # A grant wider than the page can show is refused instead of half-revoked.
    too_wide = grant_row(service, CAROL, resources, purposes=("reply",))
    assert denied(service, "sharing.preview_revoke",
                  {"kind": "memories", "identifier": "m-wide-000",
                   "grant_id": too_wide.id}) == "sharing_revoke"

    wide = grant_row(service, BOB, covered, purposes=("reply",))
    memory(service, "m-derived-wide")
    set_policy(service, "memories/m-derived-wide", classification="personal",
               sources=(covered[10],),
               pins=((covered[10], service.repository.policy(covered[10]).version),))
    assert can_read(service, BOB, covered[0]) is True
    for resource in covered[:3]:
        assert can_read(service, BOB, resource) is True

    preview = result(service, "sharing.preview_revoke",
                     {"kind": "memories", "identifier": "m-wide-000", "grant_id": wide.id})
    assert preview["scope_count"] == 100
    assert [row["resource"] for row in preview["scope"]["resources"]] == sorted(covered)
    assert all(row["remaining_basis"] == "none" for row in preview["scope"]["resources"])
    assert "memories/m-derived-wide" in preview["scope_dependents"]
    assert preview["will_apply"] is True and preview["remaining_readable"] == []

    # A covered record that this page never showed as the edit target still
    # invalidates the preview: nothing is revoked behind the administrator's back.
    changed = service.repository.policy(covered[50])
    service.repository.set_policy(replace(changed, version=secrets.token_hex(16)),
                                  changed.version, "synthetic")
    assert denied(service, "sharing.commit", {"token": preview["token"]}) == "edit_conflict"
    assert all(row["revoked"] == 0 for row in grants_of(service.store, covered[0]))

    again = result(service, "sharing.preview_revoke",
                   {"kind": "memories", "identifier": "m-wide-000", "grant_id": wide.id})
    committed = result(service, "sharing.commit", {"token": again["token"]})
    assert committed["revoked"] is True and committed["changed"] is True
    assert committed["resources"] == sorted(covered)
    assert committed["effective"] is True
    for resource in covered[:5] + covered[-5:]:
        rows = {row["id"]: row["revoked"] for row in grants_of(service.store, resource)}
        assert rows[wide.id] == 1
        assert can_read(service, BOB, resource) is False
    # The other account's wider grant is untouched.
    assert can_read(service, CAROL, covered[0]) is True


# --- grant history does not hide the current grant --------------------------


def test_grant_history_never_hides_a_current_grant(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB, CAROL)
    memory(service, "m-many")
    resource = "memories/m-many"
    set_policy(service, resource, classification="personal")
    epoch = service.repository.epoch()
    for _ in range(100):
        grant_row(service, CAROL, [resource], lifetime=-3600)          # long expired
    for _ in range(60):
        grant_row(service, CAROL, [resource], revoked=True)            # revoked
    for _ in range(49):
        grant_row(service, CAROL, [resource], epoch=epoch + 7)         # other epoch
    grant_row(service, CAROL, [resource], revoked=True)                # revoked, this epoch
    active = grant_row(service, BOB, [resource])                        # the only usable grant

    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-many"})
    assert detail["grants_total"] == 211
    assert detail["grants_active"] == 1
    assert detail["grants_revoked"] == 61
    assert detail["grants_stale_epoch"] == 49
    assert [row["id"] for row in detail["grants"]] == [active.id]
    assert detail["grants"][0]["active"] is True
    assert detail["grants_limited"] is False
    assert can_read(service, BOB, resource) is True
    assert can_read(service, CAROL, resource) is False

    # A grant still works with 210 unusable rows behind it.
    preview = result(service, "sharing.preview_grant",
                     {"kind": "memories", "identifier": "m-many", "grantee": CAROL.key,
                      "owner": service.actor.key, "purposes": ["reply"], "days": 30})
    assert preview["before"]["grants"] == 1 and preview["after"]["grants"] == 2
    assert preview["will_apply"] is True
    assert result(service, "sharing.commit", {"token": preview["token"]})["effective"] is True
    assert can_read(service, CAROL, resource) is True

    # The re-check fingerprints every grant row, not the first screenful: an
    # expired grant created after the preview still invalidates it.
    stale = result(service, "sharing.preview_grant",
                   {"kind": "memories", "identifier": "m-many", "grantee": CAROL.key,
                    "owner": service.actor.key, "purposes": ["maintenance"], "days": 30})
    count = len(grants_of(store, resource))
    grant_row(service, BOB, [resource], lifetime=-3600)
    assert len(grants_of(store, resource)) == count + 1
    assert denied(service, "sharing.commit", {"token": stale["token"]}) == "edit_conflict"
    assert len(grants_of(store, resource)) == count + 1

    memory(service, "m-crowded")
    set_policy(service, "memories/m-crowded", classification="personal")
    for _ in range(GRANT_ROWS + 5):
        grant_row(service, BOB, ["memories/m-crowded"])
    crowded = result(service, "sharing.record", {"kind": "memories", "identifier": "m-crowded"})
    assert crowded["grants_active"] == GRANT_ROWS + 5
    assert len(crowded["grants"]) == GRANT_ROWS
    assert crowded["grants_limited"] is True
    assert crowded["grants_next"] == crowded["grants"][-1]["id"]


def test_a_grant_from_another_epoch_is_not_current(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-epoch")
    set_policy(service, "memories/m-epoch", classification="personal")
    epoch = service.repository.epoch()
    current = grant_row(service, BOB, ["memories/m-epoch"])
    grant_row(service, BOB, ["memories/m-epoch"], epoch=epoch + 7)
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-epoch"})
    assert detail["grants_active"] == 1 and detail["grants_stale_epoch"] == 1
    assert [row["id"] for row in detail["grants"]] == [current.id]
    # A new governance epoch makes the previous epoch's grant inert, exactly as
    # AccessGrant.covers decides; the page must say so instead of counting it.
    service.store.set_state("governance-epoch", epoch + 1)
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-epoch"})
    assert detail["grants_active"] == 0 and detail["grants"] == []
    assert detail["grants_stale_epoch"] == 2


# --- the source report tells the truth about cycles and depth ---------------


def test_source_cycles_are_reported_with_complete_pins(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-cycle-a")
    memory(service, "m-cycle-b")
    first, second = "memories/m-cycle-a", "memories/m-cycle-b"
    set_policy(service, first, classification="public")
    set_policy(service, second, classification="public", sources=(first,),
               pins=((first, service.repository.policy(first).version),))
    set_policy(service, first, classification="public", sources=(second,),
               pins=((second, service.repository.policy(second).version),))
    # Fully pinned both ways, so no source-version problem can explain the denial.
    assert service.repository.policy(first).source_versions == (
        (second, service.repository.policy(second).version),)
    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-cycle-a"})
    assert detail["source"]["ok"] is False
    assert "cycle" in {issue["reason"] for issue in detail["source"]["issues"]}
    assert any("循环" in line for line in detail["blockers"])
    # The core denies it, so the page must not claim the sources are healthy.
    assert service.admin.access.can_read(public_request(service, BOB), first) is False


def test_source_depth_is_reported_at_the_core_budget(pg_store, monkeypatch):
    service = service_for(pg_store)
    register(service, BOB)
    monkeypatch.setattr(service.sharing.access, "limits",
                        GraphLimits(nodes=60, edges=60, depth=3, seconds=5.0))
    previous = None
    for index in range(4):
        identifier = "m-depth-%d" % index
        memory(service, identifier)
        resource = "memories/" + identifier
        if previous is None:
            set_policy(service, resource, classification="public")
        else:
            set_policy(service, resource, classification="public", sources=(previous,),
                       pins=((previous, service.repository.policy(previous).version),))
        previous = resource
    # Three levels fit the budget.
    inside = result(service, "sharing.record", {"kind": "memories", "identifier": "m-depth-2"})
    assert inside["source"]["ok"] is True and inside["source"]["depth"] == 3
    assert service.admin.access.can_read(public_request(service, BOB), "memories/m-depth-2") is True
    # The fourth level does not, and the report says so instead of truncating.
    outside = result(service, "sharing.record", {"kind": "memories", "identifier": "m-depth-3"})
    assert outside["source"]["ok"] is False
    assert "depth" in {issue["reason"] for issue in outside["source"]["issues"]}
    assert any("层级" in line for line in outside["blockers"])
    assert service.admin.access.can_read(public_request(service, BOB), "memories/m-depth-3") is False


# --- pending previews stay bounded ------------------------------------------


def test_pending_previews_are_bounded_and_expire(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-previews")
    set_policy(service, "memories/m-previews", classification="personal")
    args = {"kind": "memories", "identifier": "m-previews", "grantee": BOB.key,
            "owner": service.actor.key, "purposes": ["reply"], "days": 30}
    tokens = [result(service, "sharing.preview_grant", args)["token"] for _ in range(MAX_PREVIEWS + 5)]
    assert len(service.sharing.previews) == MAX_PREVIEWS
    # The evicted preview cannot be confirmed and grants nothing.
    assert denied(service, "sharing.commit", {"token": tokens[0]}) == "sharing_preview_expired"
    assert grants_of(service.store, "memories/m-previews") == []
    # A stale entry is dropped the next time a preview is issued.
    stale = tokens[-1]
    service.sharing.previews[stale]["expires"] = 0
    assert denied(service, "sharing.commit", {"token": stale}) == "sharing_preview_expired"
    result(service, "sharing.preview_grant", args)
    assert stale not in service.sharing.previews
    assert len(service.sharing.previews) == MAX_PREVIEWS


# --- every current grant stays operable through bounded pages ---------------


def test_grant_pages_reach_every_current_grant(pg_store):
    service = service_for(pg_store)
    register(service, BOB)
    memory(service, "m-page-grants")
    resource = "memories/m-page-grants"
    set_policy(service, resource, classification="personal")
    created = [grant_row(service, BOB, [resource]).id for _ in range(GRANT_ROWS + 5)]
    assert len(set(created)) == GRANT_ROWS + 5

    detail = result(service, "sharing.record", {"kind": "memories", "identifier": "m-page-grants"})
    assert detail["grants_active"] == GRANT_ROWS + 5
    assert len(detail["grants"]) == GRANT_ROWS
    assert detail["grants_next"] == detail["grants"][-1]["id"]
    seen, cursor, pages = [row["id"] for row in detail["grants"]], detail["grants_next"], 1
    while cursor:
        page = result(service, "sharing.grants",
                      {"kind": "memories", "identifier": "m-page-grants", "after": cursor})
        pages += 1
        assert pages < 6
        assert page["counts"]["active"] == GRANT_ROWS + 5
        assert page["counts"]["total"] == GRANT_ROWS + 5
        seen.extend(row["id"] for row in page["grants"])
        assert page["next"] != cursor
        cursor = page["next"]
    assert sorted(seen) == sorted(created)
    assert len(set(seen)) == GRANT_ROWS + 5

    # The grant behind the first page is reachable and can really be revoked.
    second = result(service, "sharing.grants",
                    {"kind": "memories", "identifier": "m-page-grants",
                     "after": detail["grants_next"]})
    behind = second["grants"][0]
    assert behind["id"] in created
    proposal = result(service, "sharing.preview_revoke",
                      {"kind": "memories", "identifier": "m-page-grants", "grant_id": behind["id"]})
    assert result(service, "sharing.commit", {"token": proposal["token"]})["revoked"] is True
    after = result(service, "sharing.record", {"kind": "memories", "identifier": "m-page-grants"})
    assert after["grants_active"] == GRANT_ROWS + 4
    assert behind["id"] not in {row["id"] for row in after["grants"]}
    # Only current grants are listed, so a bad cursor is the only page failure.
    for bad in ({"kind": "memories", "identifier": "m-page-grants", "after": None},
                {"kind": "memories", "identifier": "m-page-grants", "after": "not/a/grant"},
                {"kind": "memories", "identifier": "m-page-grants", "after": "g" * 65}):
        assert denied(service, "sharing.grants", bad) == "sharing_query"


# --- one revoke preview agrees on the whole shared scope --------------------


def test_a_revoke_preview_covers_the_whole_shared_scope(pg_store):
    store = pg_store
    service = service_for(store)
    register(service, BOB, CAROL)
    resources = ["memories/scope-a", "memories/scope-b", "memories/scope-c"]
    for resource in resources:
        memory(service, resource.split("/")[1])
        set_policy(service, resource, classification="restricted")
    grant = grant_row(service, BOB, resources, purposes=("reply", "maintenance"))

    def preview():
        return result(service, "sharing.preview_revoke",
                      {"kind": "memories", "identifier": "scope-a", "grant_id": grant.id})

    # 1. A new downstream record of a *different* covered record expands the scope.
    proposal = preview()
    memory(service, "scope-dependent")
    parent = service.repository.policy(resources[1])
    set_policy(service, "memories/scope-dependent", classification="public",
               sources=(resources[1],), pins=((resources[1], parent.version),))
    assert denied(service, "sharing.commit", {"token": proposal["token"]}) == "edit_conflict"
    assert can_read(service, BOB, resources[0]) is True

    # 2. A changed source state of another covered record changes it too.
    source_row(service, "governance/scope-source", version="s1")
    proposal = preview()
    other = service.repository.policy(resources[2])
    service.repository.set_policy(replace(other, sources=("governance/scope-source",),
                                          source_versions=(("governance/scope-source", "s1"),)),
                                  other.version, "synthetic")
    assert denied(service, "sharing.commit", {"token": proposal["token"]}) == "edit_conflict"

    # 3. Another covered record crossing its expiry is a change as well.
    proposal = preview()
    expiring = service.repository.policy(resources[1])
    service.repository.set_policy(replace(expiring, expires_at=time.time() - 1),
                                  expiring.version, "synthetic")
    assert denied(service, "sharing.commit", {"token": proposal["token"]}) == "edit_conflict"

    # 4. The same number of other grants, but a different one: counts are not enough.
    replacement_source = grant_row(service, CAROL, [resources[1]])
    proposal = preview()
    with store.db() as db:
        db.execute("DELETE FROM grant_resources WHERE grant_id=?", (replacement_source.id,))
        db.execute("DELETE FROM governance_grants WHERE id=?", (replacement_source.id,))
    replacement = grant_row(service, CAROL, [resources[1]], purposes=("maintenance",))
    assert replacement.id != replacement_source.id
    assert denied(service, "sharing.commit", {"token": proposal["token"]}) == "edit_conflict"

    # Nothing was revoked by the refused commits, and a fresh preview does it.
    assert can_read(service, BOB, resources[0]) is True
    final = preview()
    committed = result(service, "sharing.commit", {"token": final["token"]})
    assert committed["revoked"] is True and committed["resources"] == resources
    assert all(can_read(service, BOB, resource) is False for resource in resources)
