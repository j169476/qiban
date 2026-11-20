import pytest

from qiban.desktop_profile import desktop_profile
from tests_postgres.test_desktop_documents import prepare
from tests_postgres.test_desktop_facts import correction, current_fact, history, seed
from tests_postgres.test_desktop_management import call, desktop_store, memory_args, service
from tests_postgres.test_desktop_relation_claims import adjudicate, declare

__all__ = ["desktop_store", "service"]


@pytest.fixture
def pg_profile():
    return desktop_profile()


def result(service, operation, args):
    response = call(service, operation, args)
    assert response["ok"], response
    return response["result"]


@pytest.mark.parametrize("kind", ["memories", "knowledge", "diaries"])
def test_document_history_and_confirmed_restore_preserve_source(service, kind):
    if kind == "memories":
        args = memory_args()
        first = result(service, "records.correct", args)
        args.update(expected=first["version"], value={**first["data"], "content": "改过的记忆"})
    else:
        first, args = prepare(service, kind)
    base = {"identifier": args["identifier"], "kind": kind}
    second = result(service, "records.correct", args)
    page = result(service, "records.versions", {**base, "after": "", "limit": 1})
    assert page["items"][0]["version"] == second["version"] and page["original"] == first["version"]
    page2 = result(service, "records.versions", {**base, "after": page["next"], "limit": 1})
    assert page2["items"][0]["version"] == first["version"] and page2["next"] is None
    target = {**base, "version": first["version"]}
    assert result(service, "records.version", target) == first
    restore = {**target, "expected": second["version"]}
    restored = result(service, "records.restore_version", restore)
    assert restored["data"] == first["data"]
    assert call(service, "records.restore_version", restore)["error"] == "edit_conflict"
    assert call(service, "records.versions", {**base, "after": page["next"], "limit": 1})["error"] == "edit_conflict"
    before = service.repository.policy(kind + "/" + base["identifier"])
    result(service, "records.restore_version", {**target, "expected": restored["version"]})
    assert service.repository.policy(kind + "/" + base["identifier"]) == before
    service.unlocked = False
    assert call(service, "records.version", target)["error"] == "desktop_locked"


def test_fact_restore_appends_new_value_to_current_timeline(service):
    seed(service.store)
    original = current_fact(service)
    new_id = result(service, "facts.correct", correction(service, original["id"]))["id"]
    rev = history(service, new_id)["result"]["revision"]
    args = {"identifier": new_id, "version": original["id"], "expected": rev, "reason": "恢复上次核对的值",
            "supersedes": [new_id]}
    restored = result(service, "facts.restore_version", args)
    assert current_fact(service)["id"] == restored["id"] and current_fact(service)["value"] == original["value"]
    assert call(service, "facts.restore_version", args)["error"] == "edit_conflict"
    assert call(service, "records.versions", {"kind": "memories", "identifier": original["id"],
                                            "after": "", "limit": 5})["error"] == "fact_append_required"


def test_relation_history_restores_text_and_status_together(service):
    claim, _ = declare(service, reason="声明原话")
    identifier = claim["id"]
    args = {"identifier": identifier, "expected": claim["version"], "relation": "旧同事",
            "declared_reason": "目前情况已核对", "reason": "人工更正"}
    def detail():
        return result(service, "relations.detail", {"identifier": identifier})["claim"]
    proposed = result(service, "relations.text_preview", args)
    assert proposed["before"]["text"]["reason"] == "声明原话"
    assert detail()["relation"] == "同事"
    result(service, "relations.commit", {"token": proposed["token"]})
    edited = detail()
    assert edited["relation"] == "旧同事" and edited["original_declaration"]["reason"] == "声明原话"
    assert call(service, "relations.text_preview", args)["error"] == "edit_conflict"
    adjudicate(service, identifier, "confirmed")
    restore = {"identifier": identifier, "expected": detail()["version"], "sequence": 1, "side": "before", "reason": "撤回编辑"}
    proposed = result(service, "relations.restore_preview", restore)
    assert proposed["after"]["status"] == "claimed" and proposed["after"]["text"]["relation"] == "同事"
    result(service, "relations.commit", {"token": proposed["token"]})
    final = detail()
    assert final["declared_reason"] == "声明原话" and final["status"] == "claimed" and not final["admin_edited"]
    past = result(service, "relations.history", {"identifier": identifier, "after": "", "limit": 10})
    assert [x["sequence"] for x in past["items"]] == [1, 2, 3]
    assert past["items"][0]["after"]["text"] == {"relation": "旧同事", "reason": "目前情况已核对"}
