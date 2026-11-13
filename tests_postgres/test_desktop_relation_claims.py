"""REL-168 interpersonal claim adjudication on a disposable PostgreSQL schema.

Every account, event, statement, reason and identifier below is synthetic. No
connector, platform process, real chat, credential or model runs; nothing here
reads or writes the real data directory.

The page only changes the status of a claim that already exists, so these tests
check the boundaries that make that safe: the statement/evidence/owners/pins/
expiry survive, an evidence excerpt stays tied to the original reference
version, a revoked claim keeps its existing "can be restored" semantics, the
history is immutable and paged, and no account link, reply authorization or
platform registration changes.
"""
import json
import secrets
import uuid
from dataclasses import replace

import pytest

from companion_core import events, relation_history
from companion_core.access import AccessGrant, RecordPolicy
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.postgres_transfer import create, fingerprint, restore, unpack
from companion_core.relation_claims import EVIDENCE_MAX, EXCERPT_MAX, PAGE_MAX
from companion_core.relations import RelationService
from qiban.desktop_management import DesktopManagement, provision_owner
from tests_postgres.test_desktop_reply_quota import call, denied, result

INSTALLATION = "installation-relation-claims"
SID = "S-1-5-21-relation-claims-owner"
ALICE = ActorRef("qq", "11001", "qq-bot")
BOB = ActorRef("wechat", "wxid_relation", "wechat-host")
PASSWORD = "Synthetic-Relation-Password-2026"


# --- fixtures ---------------------------------------------------------------


def service_for(store):
    provision_owner(store, INSTALLATION, SID)
    return DesktopManagement(store, INSTALLATION, SID)


def relation_service(service):
    return RelationService(service.repository, service.authority)


def event_for(store, actor, native_id="m1", text="合成的证据原文"):
    event = Event(actor.platform, "bot-" + actor.platform, native_id, actor,
                  "room-" + actor.user_id, "2026-11-09T00:00:00Z", text, "private")
    events.ingest(store, event)
    return event


def declare(service, *, subject=ALICE, object=BOB, relation="同事", texts=("合成的证据原文",),
            reason=""):
    """One real claim through the core, with one event per statement of evidence."""
    for actor in (subject, object):
        relation_service(service).register_actor(actor)
    sources = [event_for(service.store, subject, native_id="m" + str(index), text=text)
               for index, text in enumerate(texts)]
    request = service.authority.issue(actor=subject, conversation="room-" + subject.user_id,
                                     audience="private", recipients=(subject,), lifetime=60)
    created = relation_service(service).propose(
        request, subject=subject.key, object=object.key, relation=relation,
        evidence=["event/" + item.id for item in sources], reason=reason)
    return created, sources


def preview(service, identifier, status, reason=""):
    return result(service, "relations.preview",
                  {"identifier": identifier, "status": status, "reason": reason})


def adjudicate(service, identifier, status, reason="", request_id=None):
    token = preview(service, identifier, status, reason)["token"]
    return result(service, "relations.commit", {"token": token}, request_id)


def claim_row(store, identifier):
    with store.db() as db:
        row = db.execute("SELECT * FROM relationship_claims WHERE id=?", (identifier,)).fetchone()
    return None if row is None else dict(row)


def policy_row(store, resource):
    with store.db() as db:
        row = db.execute("SELECT * FROM governance_policies WHERE resource=?", (resource,)).fetchone()
    return None if row is None else dict(row)


def history_keys(store, identifier):
    with store.db() as db:
        return [row[0] for row in db.execute(
            "SELECT key FROM state WHERE key LIKE ? ORDER BY key",
            ("relation-adjudication:" + identifier + ":%",)).fetchall()]


def query_ids(service, actor=ALICE, *, depth=1):
    request = service.authority.issue(actor=actor, conversation="room-" + actor.user_id,
                                     audience="private", recipients=(actor,), lifetime=60)
    return [item["id"] for item in relation_service(service).query(request, actor.key, depth=depth)["claims"]]


def can_read(service, actor, resource):
    request = service.authority.issue(actor=actor, conversation="room-" + actor.user_id,
                                     audience="private", recipients=(actor,), purpose="reply",
                                     lifetime=60)
    return service.admin.access.can_read(request, resource)


def snapshot(store):
    """Everything an adjudication must not touch, plus the state that is not history."""
    with store.db() as db:
        claims = [tuple(row) for row in db.execute(
            "SELECT id,subject,object,declarer,status,version,body FROM relationship_claims "
            "ORDER BY id").fetchall()]
        policies = [tuple(row) for row in db.execute(
            "SELECT resource,version,classification,owners,conversation,audience,sources,"
            "source_versions,valid,expires_at FROM governance_policies ORDER BY resource").fetchall()]
        content = [tuple(row) for row in db.execute(
            "SELECT kind,id,version FROM content_records ORDER BY kind,id").fetchall()]
        grants = [tuple(row) for row in db.execute(
            "SELECT id,grantee,revoked FROM governance_grants ORDER BY id").fetchall()]
        links = [tuple(row) for row in db.execute(
            "SELECT id,status,version FROM core_identity_links ORDER BY id").fetchall()]
        states = [row[0] for row in db.execute("SELECT key FROM state ORDER BY key").fetchall()]
    return {"claims": claims, "policies": policies, "content": content, "grants": grants,
            "links": links,
            "states": [key for key in states
                       if not key.startswith("desktop-request:")
                       and not key.startswith(relation_history.PREFIX)]}


def platform_registration(store):
    """A synthetic platform registration, a cross-account identity link and a grant."""
    store.set_state("desktop-qq-account", {"revision": 3, "selected": {"uin": "11001"},
                                           "configurations": {"11001": {"uid": "u_synthetic"}}})
    with store.db() as db:
        db.execute("INSERT INTO core_identity_links VALUES(?,?,?,?,?)",
                   ("link-synthetic", ALICE.key, BOB.key, "confirmed", "v1"))


def grant_on(service, resource, grantee=BOB):
    grant = AccessGrant(secrets.token_hex(16), grantee.key, ALICE.key, frozenset((resource,)),
                        frozenset(("reply",)), "owner_admin", "synthetic-relation-test",
                        service.repository.clock() + 3600, service.repository.epoch())
    service.repository.put_grant(grant, "synthetic")
    return grant


def audit_count(store, operation):
    with store.db() as db:
        return db.execute("SELECT COUNT(*) FROM governance_audit WHERE operation=?",
                          (operation,)).fetchone()[0]


# --- existing status semantics ----------------------------------------------


def test_claimed_confirmed_disputed_revoked_and_restore_keep_the_core_semantics(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    resource = "relation/" + identifier
    assert created["status"] == "claimed"
    claim, policy = claim_row(store, identifier), policy_row(store, resource)
    assert claim["status"] == "claimed" and bool(policy["valid"]) is True
    assert claim["version"] == policy["version"] == created["version"]
    assert query_ids(service) == [identifier]

    detail = result(service, "relations.detail", {"identifier": identifier})
    assert detail["claim"]["status"] == "claimed"
    assert detail["policy"]["valid"] is True
    assert detail["check"] == {"consistent": True, "anomaly": None, "detail": None}
    assert detail["claim"]["queryable"] is True and detail["claim"]["restorable"] is False
    assert detail["readable"]["allowed"] is True
    pins = policy["source_versions"]

    confirmed = adjudicate(service, identifier, "confirmed", "合成管理员说明")
    assert confirmed["changed"] is True and confirmed["state_changed"] is True
    assert confirmed["after"]["status"] == "confirmed" and confirmed["after"]["valid"] is True
    assert confirmed["version"] != created["version"]
    claim = claim_row(store, identifier)
    policy = policy_row(store, resource)
    # Only the claim status, its version and the status-derived valid flag moved.
    assert claim["status"] == "confirmed" and claim["version"] == confirmed["version"]
    assert policy["version"] == claim["version"] and bool(policy["valid"]) is True
    assert policy["source_versions"] == pins
    assert query_ids(service) == [identifier]

    # A disputed claim is still returned by the core graph query, with its status.
    disputed = adjudicate(service, identifier, "disputed")
    assert disputed["after"]["status"] == "disputed"
    request = service.authority.issue(actor=ALICE, conversation="room-" + ALICE.user_id,
                                     audience="private", recipients=(ALICE,), lifetime=60)
    claims = relation_service(service).query(request, ALICE.key)["claims"]
    assert [item["id"] for item in claims] == [identifier]
    assert [item["status"] for item in claims] == ["disputed"]

    # Revoking makes the claim node invalid and removes it from the core query.
    revoked = adjudicate(service, identifier, "revoked")
    assert revoked["after"]["valid"] is False and revoked["after"]["queryable"] is False
    claim = claim_row(store, identifier)
    policy = policy_row(store, resource)
    assert claim["status"] == "revoked" and bool(policy["valid"]) is False
    assert query_ids(service) == []
    assert can_read(service, ALICE, resource) is False

    # The existing restore semantics are kept: revoked -> confirmed is allowed again,
    # and the preview names it as a restoration instead of a plain status change.
    restored_preview = preview(service, identifier, "confirmed", "恢复合成声明")
    assert restored_preview["written"] is False
    assert restored_preview["restored"] is True and restored_preview["after"]["restores"] is True
    assert "恢复此声明" in restored_preview["notice"]
    assert "已失效或已更换的来源不会因此重新可读" in restored_preview["notice"]
    assert "来源版本不会被重新固定" in restored_preview["notice"]
    restored = result(service, "relations.commit", {"token": restored_preview["token"]})
    assert restored["restored"] is True and restored["changed"] is True
    claim = claim_row(store, identifier)
    policy = policy_row(store, resource)
    assert claim["status"] == "confirmed" and bool(policy["valid"]) is True
    # A restore does not re-pin sources.
    assert policy["source_versions"] == pins
    assert query_ids(service) == [identifier]

    # An ordinary same-status request is refused and never bumps the version.
    assert denied(service, "relations.preview",
                  {"identifier": identifier, "status": "confirmed", "reason": ""}) == "relation_status"
    before = claim_row(store, identifier)["version"]
    with pytest.raises(AssistantError):
        relation_service(service).adjudicate(service.request(), identifier, "confirmed",
                                             before, reason="同状态")
    assert claim_row(store, identifier)["version"] == before
    # Four decisions were recorded; the refused same-status attempts added none.
    assert len(history_keys(store, identifier)) == 4


def test_statement_evidence_owners_pins_and_expiry_survive_adjudication(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service, texts=("第一条合成证据", "第二条合成证据"))
    identifier = created["id"]
    resource = "relation/" + identifier
    # Add an expiry the way the core keeps it: only expires_at moves.
    current = service.repository.policy(resource)
    expiry = service.repository.clock() + 86400
    service.repository.set_policy(replace(current, expires_at=expiry), current.version, "synthetic")
    before_claim = claim_row(store, identifier)
    before_policy = policy_row(store, resource)
    platform_registration(store)
    grant_on(service, resource)
    before = snapshot(store)

    adjudicate(service, identifier, "confirmed")
    adjudicate(service, identifier, "disputed")

    after_claim = claim_row(store, identifier)
    after_policy = policy_row(store, resource)
    # The statement body is byte-identical: adjudication never rewrites it.
    assert after_claim["body"] == before_claim["body"]
    assert after_claim["subject"] == before_claim["subject"] == ALICE.key
    assert after_claim["object"] == before_claim["object"] == BOB.key
    assert after_claim["declarer"] == before_claim["declarer"] == ALICE.key
    # Owners/classification/conversation/audience/sources/pins/expiry are untouched.
    for field in ("classification", "owners", "conversation", "audience", "sources",
                  "source_versions", "expires_at"):
        assert after_policy[field] == before_policy[field], field
    assert after_policy["expires_at"] == expiry
    assert len(json.loads(after_policy["source_versions"])) == 2

    after = snapshot(store)
    assert after["content"] == before["content"]
    assert after["grants"] == before["grants"] and len(after["grants"]) == 1
    assert after["links"] == before["links"] and len(after["links"]) == 1
    assert after["states"] == before["states"]
    registration = store.state("desktop-qq-account")
    assert registration["revision"] == 3 and registration["selected"] == {"uin": "11001"}
    # Only the relation policy moved; every other policy is identical.
    assert [row for row in after["policies"] if row[0] != resource] == \
        [row for row in before["policies"] if row[0] != resource]
    assert [row for row in after["claims"] if row[0] != identifier] == \
        [row for row in before["claims"] if row[0] != identifier]


def test_a_reply_grant_and_an_identity_link_are_not_touched_by_adjudication(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    resource = "relation/" + created["id"]
    platform_registration(store)
    grant = grant_on(service, resource)
    before = snapshot(store)
    assert adjudicate(service, created["id"], "confirmed")["changed"] is True
    adjudicate(service, created["id"], "revoked")
    after = snapshot(store)
    assert after["grants"] == before["grants"]
    assert after["links"] == before["links"]
    assert after["content"] == before["content"]
    with store.db() as db:
        row = db.execute("SELECT grantee,revoked,expires_at FROM governance_grants WHERE id=?",
                         (grant.id,)).fetchone()
    assert row["grantee"] == BOB.key and row["revoked"] == 0
    assert row["expires_at"] > service.repository.clock()


# --- evidence: the original reference version, not today's text -------------


def test_evidence_is_quoted_only_while_the_pinned_version_matches(pg_store):
    store = pg_store
    service = service_for(store)
    text = "证据原文" * 600
    created, sources = declare(service, texts=(text,))
    identifier = created["id"]
    resource = "relation/" + identifier
    pinned = sources[0]
    with store.db() as db:
        pinned_version = db.execute("SELECT version FROM core_events WHERE id=?",
                                    (pinned.id,)).fetchone()[0]
    detail = result(service, "relations.detail", {"identifier": identifier})
    item = detail["evidence"]["items"][0]
    assert item["state"] == "matched" and item["matched"] is True
    assert item["pinned_version"] == pinned_version == item["current_version"]
    assert len(item["excerpt"]) == EXCERPT_MAX and item["truncated"] is True
    assert item["excerpt"] == text[:EXCERPT_MAX]

    # The event is replaced: same id, new version and new text.
    events.ingest(store, Event(pinned.platform, pinned.account, pinned.native_id, pinned.actor,
                               pinned.conversation, pinned.occurred_at, "后来改过的正文", "private"))
    detail = result(service, "relations.detail", {"identifier": identifier})
    changed = detail["evidence"]["items"][0]
    assert changed["state"] == "changed" and changed["matched"] is False
    assert changed["excerpt"] is None
    assert changed["current_version"] != changed["pinned_version"]
    assert changed["current_version"] is not None
    assert "后来改过的正文" not in repr(detail)
    assert "当前版本已更换" in changed["state_label"]
    assert detail["readable"]["allowed"] is False
    assert query_ids(service) == []
    # The source report names the change rather than hiding it.
    assert [issue["reason"] for issue in detail["source"]["issues"]] == ["source_changed"]

    # Confirming the claim keeps the status honest but never makes the source readable.
    committed = adjudicate(service, identifier, "confirmed")
    assert committed["state_changed"] is True
    assert committed["after"]["valid"] is True and committed["usable"] is False
    assert committed["readable"]["allowed"] is False
    assert "不能把它作为依据" in committed["readable_notice"]
    assert query_ids(service) == [] and can_read(service, ALICE, resource) is False
    after = result(service, "relations.detail", {"identifier": identifier})
    assert after["evidence"]["items"][0]["state"] == "changed"
    assert after["evidence"]["items"][0]["excerpt"] is None


def test_evidence_excerpts_are_bounded_by_item_and_by_page(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service, texts=tuple("证据%02d" % index + "字" * 2500
                                                     for index in range(EVIDENCE_MAX)))
    detail = result(service, "relations.detail", {"identifier": created["id"]})
    evidence = detail["evidence"]
    assert evidence["limit"] == EVIDENCE_MAX and evidence["count"] == EVIDENCE_MAX
    assert evidence["excerpt_chars"] == 12000
    assert evidence["excerpt_length"] == EXCERPT_MAX and evidence["excerpt_total"] == 12000
    with_excerpt = [item for item in evidence["items"] if item["excerpt"]]
    assert len(with_excerpt) == 6
    assert all(len(item["excerpt"]) == EXCERPT_MAX and item["truncated"] is True
               for item in with_excerpt)
    limited = [item for item in evidence["items"] if item["page_limited"] is True]
    assert len(limited) == EVIDENCE_MAX - 6
    assert all(item["excerpt"] is None and item["matched"] is True for item in limited)
    assert all("本页摘录总量已达上限" in item["state_label"] for item in limited)


def test_a_deleted_event_is_named_and_never_reconstructed(pg_store):
    store = pg_store
    service = service_for(store)
    created, sources = declare(service)
    events.forget(store, sources[0].id)
    detail = result(service, "relations.detail", {"identifier": created["id"]})
    item = detail["evidence"]["items"][0]
    assert item["state"] == "missing" and item["excerpt"] is None and item["matched"] is False
    assert "已不存在或已删除" in item["state_label"]
    assert detail["source"]["ok"] is False


def test_an_expired_source_is_reported_without_blocking_the_status_change(pg_store):
    store = pg_store
    service = service_for(store)
    created, sources = declare(service)
    # Only the expiry moves, without rotating the source policy version, so this is a
    # pure expiry: the pin still matches and the source is refused for being expired.
    with store.db() as db:
        expired = service.repository.clock() - 60
        db.execute("UPDATE governance_policies SET expires_at=? WHERE resource=?",
                   (expired, "event/" + sources[0].id))
    detail = result(service, "relations.detail", {"identifier": created["id"]})
    assert [issue["reason"] for issue in detail["source"]["issues"]] == ["expired"]
    assert detail["evidence"]["items"][0]["state"] == "matched"
    assert detail["readable"]["allowed"] is False
    committed = adjudicate(service, created["id"], "confirmed")
    assert committed["state_changed"] is True and committed["usable"] is False
    assert query_ids(service) == []


# --- preview / commit -------------------------------------------------------


@pytest.mark.parametrize("changed", ["token", "claim", "policy", "source", "indirect", "dependency"])
def test_a_preview_token_only_commits_what_was_previewed(pg_store, changed):
    store = pg_store
    service = service_for(store)
    created, sources = declare(service)
    identifier = created["id"]
    resource = "relation/" + identifier
    indirect = None
    if changed == "indirect":
        # The direct source stays byte-identical; a source below it is what moves.
        indirect = RecordPolicy("knowledge/indirect-parent", secrets.token_hex(16), "public")
        service.repository.set_policy(indirect, None, "synthetic")
        current = service.repository.policy("event/" + sources[0].id)
        service.repository.set_policy(
            replace(current, sources=(indirect.resource,),
                    source_versions=((indirect.resource, indirect.version),)),
            current.version, "synthetic")
    token = preview(service, identifier, "confirmed")["token"]
    if changed == "token":
        token = secrets.token_urlsafe(24)
    elif changed == "claim":
        relation_service(service).adjudicate(service.request(), identifier, "disputed",
                                             created["version"], reason="另一入口")
    elif changed == "policy":
        current = service.repository.policy(resource)
        service.repository.set_policy(replace(current, version=secrets.token_hex(16)),
                                      current.version, "synthetic")
    elif changed == "source":
        current = service.repository.policy("event/" + sources[0].id)
        service.repository.set_policy(replace(current, version=secrets.token_hex(16)),
                                      current.version, "synthetic")
    elif changed == "indirect":
        service.repository.set_policy(replace(indirect, version=secrets.token_hex(16)),
                                      indirect.version, "synthetic")
    else:
        value = {"schema_version": 1, "id": "m-derived", "kind": "user_preference",
                 "content": "合成的下游资料", "confidence": "observed", "tags": [], "locked": False,
                 "source": {"type": "user_instruction", "reference": "synthetic-relation",
                            "recorded_at": "2026-11-09T00:00:00Z"}}
        assert call(service, "records.correct",
                    {"kind": "memories", "identifier": "m-derived", "expected": None,
                     "value": value})["ok"]
        pinned = service.repository.policy(resource)
        service.repository.set_policy(
            RecordPolicy("memories/m-derived", secrets.token_hex(16), "personal",
                         frozenset((ALICE.key,)), "", "private", (resource,), True, None,
                         ((resource, pinned.version),)),
            service.repository.policy("memories/m-derived").version, "synthetic")
    reply = call(service, "relations.commit", {"token": token})
    assert not reply["ok"], reply
    assert reply["error"] in ("edit_conflict", "relation_preview_expired")
    claim = claim_row(store, identifier)
    if changed == "claim":
        # Only this branch deliberately wrote before the commit was attempted.
        assert claim["status"] == "disputed"
        assert claim["version"] != created["version"]
    else:
        assert claim["status"] == "claimed" and claim["version"] == created["version"]
        assert history_keys(store, identifier) == []
    assert audit_count(store, "desktop_relation_adjudicate") == 0


def test_a_lock_a_restart_and_an_expired_preview_cannot_be_committed(pg_store, monkeypatch):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]

    # Restarting the app builds a new preview set: the old token is gone.
    token = preview(service, identifier, "confirmed")["token"]
    reopened = DesktopManagement(store, INSTALLATION, SID)
    assert denied(reopened, "relations.commit", {"token": token}) == "relation_preview_expired"
    assert claim_row(store, identifier)["status"] == "claimed"

    # Locking and unlocking changes the session, so the preview dies with it.
    token = preview(reopened, identifier, "confirmed")["token"]
    reopened.enable_lock(PASSWORD)
    reopened.lock()
    assert denied(reopened, "relations.commit", {"token": token}) == "desktop_locked"
    reopened.unlock(PASSWORD)
    assert denied(reopened, "relations.commit", {"token": token}) == "relation_preview_expired"
    assert claim_row(store, identifier)["status"] == "claimed"

    # A preview that outlives its short window is refused as well.
    token = preview(reopened, identifier, "confirmed")["token"]
    monkeypatch.setattr("companion_core.relation_claims.time.monotonic", lambda: 1e12)
    assert denied(reopened, "relations.commit", {"token": token}) == "relation_preview_expired"
    assert claim_row(store, identifier)["status"] == "claimed"
    assert history_keys(store, identifier) == []


def test_commit_is_single_use_and_a_repeated_request_returns_the_first_receipt(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    token = preview(service, identifier, "confirmed", "第一次")["token"]
    key = uuid.uuid4().hex
    first = call(service, "relations.commit", {"token": token}, key)
    assert first["ok"], first
    reopened = DesktopManagement(store, INSTALLATION, SID)
    # The same request id returns the first receipt verbatim and writes nothing again.
    assert call(reopened, "relations.commit", {"token": token}, key) == first
    assert claim_row(store, identifier)["version"] == first["result"]["version"]
    assert len(history_keys(store, identifier)) == 1
    assert audit_count(store, "desktop_relation_adjudicate") == 1
    # The consumed token cannot be committed again under a new request id.
    assert denied(reopened, "relations.commit", {"token": token},
                  uuid.uuid4().hex) == "relation_preview_expired"
    assert len(history_keys(store, identifier)) == 1
    # Reusing a request id for different arguments is refused.
    another = preview(reopened, identifier, "disputed")
    assert call(reopened, "relations.commit", {"token": another["token"]}, key)["error"] == \
        "request_id_conflict"
    assert claim_row(store, identifier)["status"] == "confirmed"


def test_a_failed_receipt_rolls_the_whole_adjudication_back(pg_store, monkeypatch):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    token = preview(service, identifier, "confirmed", "合成说明")["token"]
    before = snapshot(store)
    original = store.set_state

    def fail(key, value, db=None):
        if key.startswith("desktop-request:"):
            raise AssistantError("injected", "synthetic interruption")
        return original(key, value, db)

    monkeypatch.setattr(store, "set_state", fail)
    assert denied(service, "relations.commit", {"token": token}) == "injected"
    # The status change, its policy version, its history row and the request receipt are
    # one unit of work: none of them survives the failed receipt write.
    assert snapshot(store) == before
    assert claim_row(store, identifier)["status"] == "claimed"
    assert claim_row(store, identifier)["version"] == created["version"]
    assert policy_row(store, "relation/" + identifier)["version"] == created["version"]
    assert history_keys(store, identifier) == []
    assert audit_count(store, "desktop_relation_adjudicate") == 0
    monkeypatch.setattr(store, "set_state", original)
    # The token was consumed before that outer failure, so the administrator previews
    # again instead of silently reusing a stale scope.
    assert denied(service, "relations.commit", {"token": token}) == "relation_preview_expired"
    fresh = preview(service, identifier, "confirmed", "合成说明")
    assert result(service, "relations.commit", {"token": fresh["token"]})["changed"] is True
    assert claim_row(store, identifier)["status"] == "confirmed"
    assert len(history_keys(store, identifier)) == 1


def test_cancel_writes_nothing_and_drops_the_token(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    before = snapshot(store)
    token = preview(service, identifier, "revoked", "取消")["token"]
    assert snapshot(store) == before
    cancelled = result(service, "relations.cancel", {"token": token})
    assert cancelled["cancelled"] is True and cancelled["written"] is False
    assert snapshot(store) == before
    assert denied(service, "relations.commit", {"token": token}) == "relation_preview_expired"
    assert denied(service, "relations.cancel", {"token": token}) == "relation_preview_expired"
    assert claim_row(store, identifier)["status"] == "claimed"
    assert history_keys(store, identifier) == []


# --- history ----------------------------------------------------------------


def test_history_is_paged_immutable_ordered_and_encrypted(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    steps = [("confirmed", "第一次合成说明"), ("disputed", ""), ("revoked", "第三次"),
             ("confirmed", "第四次"), ("disputed", "第五次")]
    versions = [adjudicate(service, identifier, status, reason)["version"]
                for status, reason in steps]
    keys = history_keys(store, identifier)
    assert len(keys) == len(steps)
    # Every decision is one encrypted state row, never a readable JSON array.
    with store.db() as db:
        values = [db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()[0]
                  for key in keys]
    assert all(value.startswith("enc1:") for value in values)
    assert all("第一次合成说明" not in value and "第五次" not in value for value in values)

    first = result(service, "relations.history", {"identifier": identifier, "after": "", "limit": 2})
    assert first["count"] == 2 and first["next"] and first["limit"] == 2
    second = result(service, "relations.history",
                    {"identifier": identifier, "after": first["next"], "limit": 2})
    third = result(service, "relations.history",
                   {"identifier": identifier, "after": second["next"], "limit": 2})
    assert third["next"] is None and third["count"] == 1
    items = first["items"] + second["items"] + third["items"]
    # No loss and no duplication, in write order.
    assert [item["sequence"] for item in items] == [1, 2, 3, 4, 5]
    assert [item["cursor"] for item in items] == ["%012d" % index for index in range(1, 6)]
    assert [item["after"]["status"] for item in items] == [status for status, _ in steps]
    assert [item["after"]["version"] for item in items] == versions
    assert [item["before"]["status"] for item in items] == \
        ["claimed", "confirmed", "disputed", "revoked", "confirmed"]
    assert [item["reason"] for item in items] == [reason for _, reason in steps]
    assert all(item["actor"] == service.actor.key for item in items)
    assert all(isinstance(item["at"], float) and item["at"] > 0 for item in items)
    assert all(item["unreadable"] is False for item in items)
    # Only the fourth step restores an earlier revocation.
    assert [item["restored"] for item in items] == [False, False, False, True, False]
    snapshot_row = items[0]["claim_snapshot"]
    assert snapshot_row["subject"] == ALICE.key and snapshot_row["object"] == BOB.key
    assert snapshot_row["relation"] == "同事" and snapshot_row["evidence"]
    assert items[0]["sources"] and all(len(pin) == 2 for pin in items[0]["sources"])
    # Re-reading one page returns identical entries: nothing rewrites the past.
    again = result(service, "relations.history", {"identifier": identifier, "after": "", "limit": 2})
    assert again["items"] == first["items"]
    # The detail reports the exact count and that the record covers the whole history.
    detail = result(service, "relations.detail", {"identifier": identifier})
    assert detail["history_available"] is True and detail["history"]["count"] == 5
    assert detail["history"]["complete"] is True and detail["history"]["current"] is True

    for bad in ({"identifier": identifier, "after": "x", "limit": 2},
                {"identifier": identifier, "after": "", "limit": 0},
                {"identifier": identifier, "after": "", "limit": relation_history.PAGE_MAX + 1},
                {"identifier": identifier, "after": "", "limit": True}):
        assert denied(service, "relations.history", bad) == "relation_history"
    assert denied(service, "relations.history",
                  {"identifier": "missing-claim", "after": "", "limit": 2}) == "relation_claim"


def test_decisions_older_than_this_feature_are_reported_not_invented(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    # A claim decided before this feature existed: status and validity agree, but no
    # history row was ever written for it.
    with store.db() as db:
        db.execute("UPDATE relationship_claims SET status=?,version=? WHERE id=?",
                   ("confirmed", "legacy-version-1", identifier))
    detail = result(service, "relations.detail", {"identifier": identifier})
    assert detail["history_available"] is False and detail["history"]["count"] == 0
    assert "本次管理功能开始记录之前" in detail["history"]["note"]
    assert "不虚构以前的裁决" in detail["history"]["note"]
    assert detail["check"]["consistent"] is True
    empty = result(service, "relations.history",
                   {"identifier": identifier, "after": "", "limit": 20})
    assert empty["items"] == [] and empty["next"] is None
    assert "本次管理功能开始记录之前" in empty["history"]["note"]

    # A later decision records its own before-status, so the page says where it starts.
    adjudicate(service, identifier, "disputed")
    detail = result(service, "relations.detail", {"identifier": identifier})
    assert detail["history_available"] is True and detail["history"]["count"] == 1
    assert detail["history"]["complete"] is False
    assert "最早一次记录的变更前状态不是“仅声明”" in detail["history"]["note"]
    page = result(service, "relations.history",
                  {"identifier": identifier, "after": "", "limit": 20})
    assert len(page["items"]) == 1
    assert page["items"][0]["before"]["status"] == "confirmed"
    assert page["items"][0]["after"]["status"] == "disputed"


def test_the_older_entry_point_keeps_the_same_history(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    # The web admin entry calls the core method directly, without a reason argument.
    written = relation_service(service).adjudicate(service.request(), identifier, "confirmed",
                                                   created["version"])
    assert written["status"] == "confirmed" and written["history"]["sequence"] == 1
    page = result(service, "relations.history",
                  {"identifier": identifier, "after": "", "limit": 20})
    assert len(page["items"]) == 1
    assert page["items"][0]["reason"] == ""
    assert page["items"][0]["before"]["version"] == created["version"]
    assert page["items"][0]["after"]["version"] == written["version"]
    assert page["items"][0]["actor"] == service.actor.key


def test_an_inconsistent_status_and_valid_pair_is_reported_not_repaired(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    resource = "relation/" + identifier
    # A stored pair that disagrees: the claim says revoked while its policy is valid.
    with store.db() as db:
        db.execute("UPDATE relationship_claims SET status=? WHERE id=?", ("revoked", identifier))
    before = snapshot(store)
    detail = result(service, "relations.detail", {"identifier": identifier})
    assert detail["check"]["consistent"] is False
    assert detail["check"]["anomaly"] == "status_valid_mismatch"
    assert "状态与它的权限有效标记不一致" in detail["check"]["detail"]
    assert denied(service, "relations.preview",
                  {"identifier": identifier, "status": "confirmed", "reason": ""}) \
        == "relation_inconsistent"
    with pytest.raises(AssistantError):
        relation_service(service).adjudicate(service.request(), identifier, "confirmed",
                                             claim_row(store, identifier)["version"], reason="x")
    # Nothing was written and the invalid flag was not silently turned back on.
    assert snapshot(store) == before
    assert bool(policy_row(store, resource)["valid"]) is True
    assert claim_row(store, identifier)["version"] == created["version"]
    assert history_keys(store, identifier) == []


# --- reads: bounded paging, filters, fixed API shape ------------------------


def test_listing_filters_before_the_limit_and_pages_by_id(pg_store):
    store = pg_store
    service = service_for(store)
    identifiers = []
    for index in range(5):
        created, _sources = declare(service, texts=("合成证据" + str(index),))
        identifiers.append(created["id"])
    adjudicate(service, identifiers[0], "confirmed")
    adjudicate(service, identifiers[1], "revoked")

    first = result(service, "relations.list", {"after": "", "limit": 2, "status": "all"})
    assert set(first) >= {"claims", "next", "count", "totals", "statuses"}
    assert first["count"] == 2 and first["next"] == first["claims"][-1]["id"]
    assert first["totals"] == {"claimed": 3, "confirmed": 1, "disputed": 0, "revoked": 1}
    seen, cursor = [], ""
    while True:
        page = result(service, "relations.list", {"after": cursor, "limit": 2, "status": "all"})
        seen.extend(row["id"] for row in page["claims"])
        if not page["next"]:
            break
        cursor = page["next"]
    assert sorted(seen) == sorted(identifiers) and len(set(seen)) == len(seen)

    # The status filter is applied before the page limit, not to a scanned page.
    revoked = result(service, "relations.list", {"after": "", "limit": 1, "status": "revoked"})
    assert [row["id"] for row in revoked["claims"]] == [identifiers[1]]
    assert revoked["claims"][0]["status"] == "revoked"
    assert revoked["claims"][0]["queryable"] is False
    assert result(service, "relations.list",
                  {"after": "", "limit": PAGE_MAX, "status": "disputed"})["claims"] == []
    for bad in ({"after": "", "limit": 0, "status": "all"},
                {"after": "", "limit": PAGE_MAX + 1, "status": "all"},
                {"after": "", "limit": True, "status": "all"},
                {"after": "no/slash", "limit": 2, "status": "all"},
                {"after": "", "limit": 2, "status": "unknown"}):
        assert denied(service, "relations.list", bad) == "relation_query"
    assert denied(service, "relations.list",
                  {"after": "", "limit": 2, "status": "all", "administrator": True}) \
        == "management_operation"


def test_detail_and_preview_expose_the_fixed_api_shape(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    detail = result(service, "relations.detail", {"identifier": identifier})
    for key in ("claim", "policy", "evidence", "source", "impact", "history_available",
                "history", "readable", "check", "statuses", "targets", "meaning"):
        assert key in detail, key
    # Impact counts the claim itself apart from its downstream dependents.
    assert detail["impact"]["self_count"] == 1
    assert detail["impact"]["total_count"] == detail["impact"]["affected_count"] == 1
    assert detail["impact"]["downstream_count"] == 0
    assert "这条声明自身" in detail["impact"]["meaning"]
    assert detail["impact"]["fingerprint"]
    # Registered accounts get a readable label; nothing is inferred beyond that.
    assert detail["claim"]["subject"]["registered"] is True
    assert detail["claim"]["subject"]["label"].startswith("QQ · ")
    assert detail["claim"]["object"]["label"].startswith("微信 · ")
    assert detail["claim"]["declarer"]["registered"] is True

    opened = preview(service, identifier, "confirmed", "合成说明")
    for key in ("token", "before", "after", "source", "impact", "notice", "written",
                "restored", "reason", "claim", "history", "readable_before"):
        assert key in opened, key
    assert opened["written"] is False and opened["restored"] is False
    assert opened["reason"] == "合成说明"
    assert opened["before"]["status"] == "claimed" and opened["after"]["status"] == "confirmed"
    assert opened["after"]["valid"] is True and opened["after"]["queryable"] is True
    assert opened["impact"]["downstream_count"] == 0
    # Only these three targets exist; "claimed" is not an administrative status.
    assert [row["value"] for row in detail["targets"]] == ["confirmed", "disputed", "revoked"]
    assert [row["value"] for row in detail["statuses"]] == \
        ["claimed", "confirmed", "disputed", "revoked"]

    assert denied(service, "relations.detail", {"identifier": "bad/id"}) == "relation_claim"
    assert denied(service, "relations.detail", {"identifier": "missing-claim"}) == "relation_claim"
    assert denied(service, "relations.preview",
                  {"identifier": identifier, "status": "claimed", "reason": ""}) == "relation_status"
    assert denied(service, "relations.preview",
                  {"identifier": identifier, "status": "confirmed", "reason": "字" * 1001}) \
        == "relation_reason"


def test_relation_management_requires_an_unlocked_administrator(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    service.enable_lock(PASSWORD)
    service.lock()
    for operation, arguments in (
            ("relations.list", {"after": "", "limit": 5, "status": "all"}),
            ("relations.detail", {"identifier": identifier}),
            ("relations.history", {"identifier": identifier, "after": "", "limit": 5}),
            ("relations.preview", {"identifier": identifier, "status": "confirmed", "reason": ""}),
            ("relations.commit", {"token": "x"}),
            ("relations.cancel", {"token": "x"})):
        assert denied(service, operation, arguments) == "desktop_locked", operation
    assert claim_row(store, identifier)["status"] == "claimed"
    service.unlock(PASSWORD)
    assert result(service, "relations.list",
                  {"after": "", "limit": 5, "status": "all"})["count"] == 1
    assert result(service, "relations.detail",
                  {"identifier": identifier})["claim"]["id"] == identifier


# --- the ordinary backup path carries the claim and its history together ----


def test_claim_and_encrypted_history_survive_a_real_backup_restore(pg_store):
    store = pg_store
    service = service_for(store)
    created, _sources = declare(service)
    identifier = created["id"]
    resource = "relation/" + identifier
    adjudicate(service, identifier, "confirmed", "备份前的合成说明")
    adjudicate(service, identifier, "disputed", "第二段合成说明")
    key = secrets.token_bytes(32)
    blob = create(store, key)
    # The claim history travels inside the encrypted state rows, not as readable text.
    assert "备份前的合成说明".encode() not in blob

    def keys_in(block):
        index = block["columns"].index("key")
        return [row[index] for row in block["rows"]]

    snapshot_of_backup = unpack(blob, key)
    state_keys = keys_in(snapshot_of_backup["tables"]["state"])
    assert [value for value in state_keys
            if value.startswith(relation_history.PREFIX + identifier)] != []
    claims_block = snapshot_of_backup["tables"]["relationship_claims"]
    assert any(row[claims_block["columns"].index("id")] == identifier
               for row in claims_block["rows"])

    # A decision taken after the backup must be replaced by the snapshot, together
    # with the claim, its policy and the history that names the same version.
    third = adjudicate(service, identifier, "revoked", "备份后的合成说明")
    assert len(history_keys(store, identifier)) == 3
    restored = restore(store, blob, key, expected=fingerprint(store))
    assert restored["restored"] is True
    claim, policy = claim_row(store, identifier), policy_row(store, resource)
    assert claim["status"] == "disputed"
    assert claim["version"] != third["version"]
    # No version mismatch: the restored claim, its policy and the last recorded
    # decision all describe the same revision.
    assert policy["version"] == claim["version"]
    assert bool(policy["valid"]) is True
    assert len(history_keys(store, identifier)) == 2
    page = relation_history.page(store, identifier, "", 5)
    assert [item["reason"] for item in page["items"]] == ["备份前的合成说明", "第二段合成说明"]
    assert [item["after"]["status"] for item in page["items"]] == ["confirmed", "disputed"]
    assert page["items"][-1]["after"]["version"] == claim["version"]
    assert page["items"][-1]["after"]["policy_version"] == policy["version"]
    assert relation_history.bounds(store, identifier)["count"] == 2
    # The page service agrees after the restore (restoring revokes sessions, so a new
    # sealed request is issued and the claim reads back exactly as it was backed up).
    detail = result(service, "relations.detail", {"identifier": identifier})
    assert detail["claim"]["status"] == "disputed"
    assert detail["claim"]["version"] == claim["version"]
    assert detail["history"]["count"] == 2 and detail["history"]["complete"] is True
    assert detail["policy"]["valid"] is True
