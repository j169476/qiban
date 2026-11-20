import pytest

from qiban.desktop_context_sources import growth_scope
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service
from tests_postgres.test_desktop_workspace import pg_profile as pg_profile
from tests_postgres.test_desktop_workspace import read


def test_preview_uses_unsaved_changes_without_writing_a_draft(service):
    initial = read(service, "context.workspace")
    config = {**initial["active"]["config"], "disabled": ["summary"]}
    result = read(service, "context.preview_config", {"scene": "private", "config": config})
    assert result["reports"]["draft"]["sections"]["summary"]["reason"] == "disabled"
    assert read(service, "context.workspace") == initial


def test_summary_switches_versions_without_replacing_evidence(service):
    doc = {"schema_version": 1, "id": "summary-1", "account_uid": "123", "thread_id": "thread-1",
           "content": "初次约的时间", "turn_refs": [{"id": "turn-1", "version": "0" * 64}], "previous": None,
           "source": {"type": "reflection", "reference": "thread-1", "recorded_at": "2026-11-20T00:00:00Z"},
           "locked": False}
    service.store.write("conversation_summaries", doc, None, "fixture", doc["id"])
    target = {"layer": "summary", "id": doc["id"]}
    assert read(service, "context.source_list", {"layer": "summary", "after": None})["items"][0]["preview"] == doc["content"]
    before = read(service, "context.source_detail", target)
    change = {**target, "expected": before["version"], "fields": {"content": "改到明天"}}
    after = read(service, "context.source_save", change)
    assert call(service, "context.source_save", change)["error"] == "edit_conflict"
    history = read(service, "context.source_versions", {**target, "after": None})
    original = read(service, "context.source_version", {**target, "version": history["original"]})
    assert original["fields"] == before["fields"]
    read(service, "context.source_save", {**target, "expected": after["version"], "fields": original["fields"]})
    assert service.store.read("conversation_summaries", doc["id"])["data"] == doc
    service.unlocked = False
    assert call(service, "context.source_versions", {**target, "after": None})["error"] == "desktop_locked"


def test_growth_edits_follow_the_conversation_and_preserve_new_automatic_state(service):
    store = service.store

    def advance(actor, identifier, state):
        doc = {"schema_version": 1, "id": identifier, "actor": actor, "account": "100", "channel": "qq",
               "conversation": "chat", "source": {"type": "interaction", "reference": "evt",
               "recorded_at": "2026-11-20T01:00:00Z"}, "locked": False, "state": state}
        store.write("interaction_growth", doc, None, "fixture", identifier)
        store.set_state(growth_scope(doc), identifier)
        return {"layer": "growth", "id": growth_scope(doc)}

    state = {"interests": {}, "traits": [], "opinions": [], "speech_habits": [], "pending_experiences": [],
             "interest_day": "2026-11-20", "interest_change": 0}
    target = advance("alice", "g-first", state)
    other = advance("bob", "g-other", state)
    before = read(service, "context.source_detail", target)
    changed = read(service, "context.source_save", {**target, "expected": before["version"],
                   "fields": {**before["fields"], "traits": ["耐心"]}})
    target = advance("alice", "g-next", {**state, "traits": ["耐心"], "interest_change": 0.1})
    assert call(service, "context.source_save", {**target, "expected": changed["version"],
                "fields": before["fields"]})["error"] == "edit_conflict"
    history = read(service, "context.source_versions", {**target, "after": None})
    assert history["original"] == before["version"]
    assert {v["version"] for v in history["items"]} == {history["current"], before["version"], changed["version"]}
    restored = read(service, "context.source_version", {**target, "version": history["original"]})
    assert call(service, "context.source_version", {**other, "version": restored["version"]})["error"] == "not_found"
    read(service, "context.source_save", {**target, "expected": history["current"], "fields": restored["fields"]})
    now = store.read("interaction_growth", "g-next")["data"]["state"]
    assert now["traits"] == [] and now["interest_change"] == 0.1


@pytest.mark.asyncio
async def test_next_local_reply_reads_manually_edited_growth(service):
    from tests_postgres.test_interaction_growth import prepare

    runtime, model, token = prepare(service.store)
    runtime.local.post(token, "synthetic", "a" * 32)
    await runtime.drain_local()
    record = read(service, "context.source_list", {"layer": "growth", "after": None})["items"][0]
    target = {"layer": "growth", "id": record["id"]}
    before = read(service, "context.source_detail", target)
    fields = {**before["fields"], "traits": ["先听完再回答"]}
    read(service, "context.source_save", {**target, "fields": fields, "expected": before["version"]})
    runtime.local.post(token, "synthetic", "b" * 32)
    await runtime.drain_local()
    assert model.contexts[-1]["private_growth"]["traits"] == fields["traits"]
