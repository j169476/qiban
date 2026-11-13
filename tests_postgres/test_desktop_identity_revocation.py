"""Desktop revocation of one existing same-person link reuses the core writer."""
import uuid

import pytest

from companion_core import baseline
from companion_core.contracts import ActorRef, stable_key
from companion_core.errors import AssistantError
from companion_core.identity import IdentityService
from companion_core.manual_identity import DEFAULT_REVOKE_REASON
from companion_core.person_baseline import matches
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_desktop_manual_identity import BILI, QQ, WX, confirm, pair, proof, proposal

__all__ = ["desktop_store", "service"]
SETTING = {"owner_name": "测试人物", "addresses": ["朋友🙂"], "level": "熟悉", "description": "清楚地说明数字123"}


def link_id(left, right):
    return stable_key("link", *sorted((left.key, right.key)))


def person_keys(store, actor):
    """The core returns ActorRef dicts here; only the actor keys are compared."""
    return {ActorRef(**item).key for item in IdentityService(store).person(actor)["members"]}


def existing_link(store, first, second):
    """An already-existing administrator record.

    ``manual_identity.manage`` refuses to confirm a pair that the graph already joins,
    but that does not mean such a record cannot exist; a synthetic one is written the
    same legal way the fixture of the person baseline does.
    """
    key = link_id(first, second)
    store.write("identity_links", {"schema_version": 1, "id": key, "left": first.as_dict(),
                                   "right": second.as_dict(), "status": "hard", "manual": True,
                                   "locked": True, "reason": "合成既有管理员关联",
                                   "evidence": [], "history": []}, None, "fixture", key)
    return key


def audit(store, operation):
    with store.db() as db:
        return [tuple(row) for row in db.execute(
            "SELECT actor,resource,before_version,after_version FROM governance_audit "
            "WHERE operation=? ORDER BY at", (operation,)).fetchall()]


def test_desktop_revocation_keeps_evidence_history_lock_and_audit(service):
    first = proof(service.store, QQ, WX)[0][1]
    IdentityService(service.store).apply(proposal([first]))
    assert confirm(service, QQ, WX)["ok"]
    link = link_id(QQ, WX)

    page = call(service, "relationship.links", {"after": ""})["result"]
    assert [item["id"] for item in page["links"]] == [link]
    assert {item["left"]["key"] for item in page["links"]} == {QQ.key}
    assert {item["right"]["key"] for item in page["links"]} == {WX.key}
    assert page["links"][0]["status"] == "hard" and page["links"][0]["manual"]
    assert page["links"][0]["revocable"] is True and page["next"] is None

    view = call(service, "relationship.link_detail", {"identifier": link})["result"]
    assert view["version"] == page["links"][0]["version"]
    assert view["status"] == "hard" and view["revocable"] is True
    quoted = view["evidence"]["items"][0]
    assert quoted["quote"] == first["quote"] and quoted["stored_version"] == first["version"]
    assert quoted["state"] == "stored" and quoted["current_version"] == first["version"]
    assert {item["key"] for item in view["person"]["accounts"]} == {QQ.key, WX.key}
    assert view["person"]["grants_permissions"] is False
    assert view["history"]["items"][-1]["kind"] == "manual_confirmation"
    assert view["history"]["limit"] == 100 and view["reason"]

    key = uuid.uuid4().hex
    params = {"identifier": link, "expected": view["version"], "reason": "合成管理员撤销核对"}
    result = call(service, "relationship.link_revoke", params, key)
    assert result["ok"] and result["result"]["revoked"] is True
    # The same desktop receipt returns the same result instead of writing twice.
    assert call(service, "relationship.link_revoke", params, key) == result

    saved = service.store.read("identity_links", link)
    doc = saved["data"]
    assert doc["status"] == "revoked" and doc["manual"] and doc["locked"]
    assert doc["evidence"] == [first]
    assert len(doc["history"]) == 3 and doc["history"][-1]["status"] == "revoked"
    assert doc["history"][-1]["reason"] == "合成管理员撤销核对"
    rows = audit(service.store, "revoke_identity")
    assert rows == [(service.actor.key, "identity_links/" + link, view["version"], saved["version"])]

    # The core keeps its own semantics: the revoked link cannot be written again by review,
    # and the page reports it as already revoked instead of duplicating the revocation.
    with pytest.raises(AssistantError) as exc:
        IdentityService(service.store).apply(proposal([first]))
    assert exc.value.code == "identity_locked"
    assert IdentityService(service.store).links()[0]["effective_status"] == "revoked"
    again = call(service, "relationship.links", {"after": ""})["result"]["links"][0]
    assert again["status"] == "revoked" and again["revocable"] is False
    repeated = call(service, "relationship.link_revoke",
                    {"identifier": link, "expected": saved["version"], "reason": ""})
    assert repeated["error"] == "identity_already_revoked"
    assert service.store.read("identity_links", link)["version"] == saved["version"]


def test_stale_expected_receipt_conflict_and_desktop_lock(service):
    first = proof(service.store, QQ, WX, BILI)[0][1]
    IdentityService(service.store).apply(proposal([first]))
    assert confirm(service, QQ, WX)["ok"]
    link = link_id(QQ, WX)
    view = call(service, "relationship.link_detail", {"identifier": link})["result"]
    before = service.store.read("identity_links", link)

    stale = call(service, "relationship.link_revoke",
                 {"identifier": link, "expected": "0" * len(view["version"]), "reason": ""})
    assert stale["error"] == "identity_changed"
    assert service.store.read("identity_links", link) == before

    key = uuid.uuid4().hex
    assert call(service, "relationship.link_revoke",
                {"identifier": link, "expected": view["version"], "reason": ""}, key)["ok"]
    saved = service.store.read("identity_links", link)
    # An empty reason keeps the existing administrator wording instead of blanking the field.
    assert saved["data"]["reason"] == DEFAULT_REVOKE_REASON
    # One receipt id may not be reused for another payload.
    assert call(service, "relationship.link_revoke",
                {"identifier": link, "expected": view["version"], "reason": "另一份说明"},
                key)["error"] == "request_id_conflict"
    assert service.store.read("identity_links", link) == saved

    assert confirm(service, QQ, BILI)["ok"]
    other = call(service, "relationship.link_detail", {"identifier": link_id(QQ, BILI)})["result"]
    service.enable_lock("Synthetic-Identity-Revocation-Lock!")
    service.lock()
    for operation, params in (("relationship.links", {"after": ""}),
                              ("relationship.link_detail", {"identifier": other["id"]}),
                              ("relationship.link_revoke", {"identifier": other["id"],
                               "expected": other["version"], "reason": ""})):
        assert call(service, operation, params)["error"] == "desktop_locked"
    assert service.store.read("identity_links", other["id"])["data"]["status"] == "hard"


def test_other_manual_paths_dynamic_matching_and_authorization_unchanged(service):
    event = proof(service.store, QQ, WX, BILI)[0][0]
    assert confirm(service, QQ, WX)["ok"] and confirm(service, QQ, BILI)["ok"]
    # While the two confirming records are intact the graph already joins QQ with the Bilibili account.
    assert call(service, "relationship.link_preview", pair(QQ, BILI))["error"] == "identity_already_linked"
    baseline.publish(service.store, baseline.preview(service.store), "fixture")
    args = {"anchor": QQ.key, "selected": [QQ.key, WX.key],
            "expected": service.store.read("persona")["version"], "setting": SETTING}
    assert call(service, "relationship.save", args)["ok"]
    assert [matches(service.store, actor) for actor in (QQ, WX, BILI)] == [True, True, False]
    resource = "event/" + event.id
    service.admin.authorize(service.request(), grantee=WX.key, owner=QQ.key, resources=[resource])
    grants = service.repository.grants(WX.key)
    issued = service.authority.issue(actor=WX, conversation="room-" + WX.user_id, audience="private",
                                    recipients=(WX,), purpose="reply", lifetime=60)
    assert service.admin.access.can_read(issued, resource)

    link = link_id(QQ, WX)
    view = call(service, "relationship.link_detail", {"identifier": link})["result"]
    assert call(service, "relationship.link_revoke",
                {"identifier": link, "expected": view["version"], "reason": "合成撤回"},
                uuid.uuid4().hex)["ok"]

    # Another administrator-confirmed path still makes QQ and the Bilibili account one person.
    assert person_keys(service.store, QQ) == {QQ.key, BILI.key}
    assert person_keys(service.store, WX) == {WX.key}
    members = call(service, "relationship.person", {"anchor": QQ.key})["result"]["members"]
    assert {item["key"] for item in members} == {QQ.key, BILI.key}
    # The existing conflict rule is unchanged: a revoked record touching both groups is reported,
    # exactly as the older manual-confirmation entry point already did.
    assert call(service, "relationship.link_preview", pair(QQ, BILI))["error"] == "identity_conflict"
    assert call(service, "relationship.link_preview", pair(WX, QQ))["error"] == "identity_conflict"
    # The shared setting keeps matching by the original rules and is not rewritten here.
    assert [matches(service.store, actor) for actor in (QQ, WX, BILI)] == [True, False, False]
    data = call(service, "relationship.read")["result"]
    assert data["setting"]["addresses"] == ["朋友🙂"] and set(data["selected"]) == {QQ.key, WX.key}
    # Reply authorization and the existing grant are untouched by the revocation.
    assert service.repository.grants(WX.key) == grants
    assert service.admin.access.can_read(issued, resource)


def test_revoked_endpoints_stay_connected_through_remaining_manual_records(service):
    proof(service.store, QQ, WX, BILI)  # registers the three accounts; the records below are synthetic
    revoked = existing_link(service.store, QQ, WX)
    existing_link(service.store, QQ, BILI)
    existing_link(service.store, WX, BILI)
    assert person_keys(service.store, QQ) == {QQ.key, WX.key, BILI.key}

    view = call(service, "relationship.link_detail", {"identifier": revoked})["result"]
    assert view["status"] == "hard" and view["revocable"] is True
    assert call(service, "relationship.link_revoke",
                {"identifier": revoked, "expected": view["version"], "reason": "合成只撤销一条"},
                uuid.uuid4().hex)["ok"]

    statuses = {item["data"]["id"]: item["effective_status"] for item in IdentityService(service.store).links()}
    assert statuses == {revoked: "revoked", link_id(QQ, BILI): "hard", link_id(WX, BILI): "hard"}
    # The two surviving administrator records still join the revoked endpoints: one link was revoked,
    # the person was not split, and the page says exactly that instead of promising a split.
    assert person_keys(service.store, QQ) == {QQ.key, WX.key, BILI.key}
    assert person_keys(service.store, WX) == {QQ.key, WX.key, BILI.key}
    members = call(service, "relationship.person", {"anchor": QQ.key})["result"]["members"]
    assert {item["key"] for item in members} == {QQ.key, WX.key, BILI.key}
    doc = service.store.read("identity_links", revoked)["data"]
    assert doc["status"] == "revoked" and doc["locked"] and doc["manual"]
    assert doc["reason"] == "合成只撤销一条"
