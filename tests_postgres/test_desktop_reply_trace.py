"""Read-only reply processing trace in the desktop app.

Every record, policy and source version here is synthetic. The disposable
PostgreSQL schema is the only dependency: no connector, platform process or model
runs, and no real database, credential or chat is read.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from companion_core import events
from companion_core.access import RequestAuthority
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import GovernedContext, RecordPolicy
from companion_core.runtime import Companion
from qiban import desktop_reply_trace as trace_reads
from tests_postgres.test_desktop_reply_quota import denied, result, service_for
from tests_postgres.test_runtime import Adapter, Host, Model

STARTED = datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc)


def new_id(at=0):
    return (STARTED + timedelta(seconds=at)).isoformat() + ":" + uuid.uuid4().hex


def seed(store, *, at=0, identifier=None, status="completed", platform="qq", duration=412,
         steps=None, sources=None, **extra):
    """One stored record with the exact key and field shape core writes."""
    value = {"id": identifier or new_id(at), "event_id": "event-synthetic-" + uuid.uuid4().hex[:8],
             "platform": platform,
             "generation": 1, "started": (STARTED + timedelta(seconds=at)).isoformat(), "status": status,
             "steps": [{"stage": "context", "memory_records": 2}, {"stage": "reply.generate",
                       "status": "success", "duration_ms": 190}] if steps is None else steps,
             "sources": list(sources or []), **extra}
    if status != "running" and "duration_ms" not in value:
        value["duration_ms"] = duration
    store.set_state("reply-trace:" + value["id"], value)
    return value


def policy(service, resource, version, *, owners=None, classification="personal", valid=True, expected=None):
    """A real policy row, so the filtering below is the unchanged AccessService."""
    service.repository.set_policy(RecordPolicy(
        resource=resource, version=version, classification=classification,
        owners=frozenset((service.actor.key,) if owners is None else owners), valid=valid), expected, "synthetic")


def detail(service, value):
    return result(service, "reply_trace.detail", {"identifier": value})


def test_recent_listing_keeps_the_core_bound_default_and_order(pg_store):
    store = pg_store
    service = service_for(store)
    newest = [seed(store, at=index)["id"] for index in range(35)]

    page = result(service, "reply_trace.list", {"limit": 30})
    assert page["limit"] == 30 and page["count"] == 30 and page["recent_only"] is True
    # Newest first, no invented cursor and no pretend paging.
    assert [row["id"] for row in page["items"]] == newest[::-1][:30]
    assert result(service, "reply_trace.list", {"limit": 1})["items"][0]["id"] == newest[-1]
    assert len(result(service, "reply_trace.list", {"limit": 100})["items"]) == 35
    # The list omits exactly what core omits and keeps the metadata the page shows.
    row = page["items"][0]
    assert not {"steps", "sources", "retention_roots"} & set(row)
    assert (row["platform"], row["status"], row["duration_ms"]) == ("qq", "completed", 412)
    assert row["started"] == (STARTED + timedelta(seconds=34)).isoformat()
    # The page default is the same 30 used by a direct call and by the web entry.
    direct = trace_reads.listing(service, service.request())
    assert direct["limit"] == 30 and direct["count"] == 30
    assert trace_reads.listing(service, service.request(), 1)["items"][0]["id"] == newest[-1]


def test_listing_rejects_values_outside_one_to_one_hundred(pg_store):
    service = service_for(pg_store)
    seed(pg_store)
    for bad in (0, 101, -1, 1000, True, False, "30", 30.0, None, [30], {"limit": 30}):
        assert denied(service, "reply_trace.list", {"limit": bad}) == "reply_trace_invalid"
    # An unknown field is refused before any record is read.
    assert denied(service, "reply_trace.list", {"limit": 30, "after": ""}) == "management_operation"
    assert denied(service, "reply_trace.list", {}) == "management_operation"
    for bad in (0, "100"):
        with pytest.raises(AssistantError) as error:
            trace_reads.listing(service, service.request(), bad)
        assert error.value.code == "reply_trace_invalid"


def test_detail_reports_source_versions_after_the_current_permission(pg_store):
    store = pg_store
    service = service_for(store)
    policy(service, "memories/owned", "v1")
    policy(service, "memories/rotated", "r2")
    policy(service, "memories/foreign", "o1", owners=("alpha:a",))
    record = seed(store, sources=[
        {"resource": "memories/owned", "version": "v1"},
        {"resource": "memories/rotated", "version": "r1"},
        {"resource": "memories/foreign", "version": "o1"}], source_count=3, sources_limited=True)

    shown = detail(service, record["id"])
    assert shown["hidden_sources"] == 1
    assert [(item["resource"], item["version"], item["version_current"]) for item in shown["sources"]] == [
        ("memories/owned", "v1", True), ("memories/rotated", "r1", False)]
    assert shown["sources_limited"] is True
    # The record body itself is untouched: same stages, same count, no retention roots.
    assert shown["steps"] == record["steps"] and shown["source_count"] == 3
    assert "retention_roots" not in shown
    assert "不保存提示词、原文或模型内部推理" in shown["meaning"]
    assert shown["status"] == "completed" and shown["duration_ms"] == 412

    # Revoking the permission hides the source on the next read; nothing is rewritten.
    policy(service, "memories/owned", "v1b", valid=False, expected="v1")
    revoked = detail(service, record["id"])
    assert revoked["hidden_sources"] == 2
    assert {item["resource"] for item in revoked["sources"]} == {"memories/rotated"}
    assert store.state("reply-trace:" + record["id"]) == record


def test_missing_unknown_and_oversized_records_are_reported_as_failures(pg_store):
    store = pg_store
    service = service_for(store)
    seed(store, at=1)

    assert denied(service, "reply_trace.detail", {"identifier": new_id(9)}) == "not_found"
    reply = service.dispatch({"action": "management", "operation": "reply_trace.detail",
                              "arguments": {"identifier": new_id(9)}, "request_id": uuid.uuid4().hex})
    assert "不存在或已过期" in reply["message"]
    # A record cleaned up by the existing retention is gone, never a blank success.
    expired = seed(store, at=2)
    with store.db() as db:
        db.execute("DELETE FROM state WHERE key=?", ("reply-trace:" + expired["id"],))
    assert denied(service, "reply_trace.detail", {"identifier": expired["id"]}) == "not_found"
    for bad in ("", "x" * 101, None, 5, ["id"], {"id": 1}):
        assert denied(service, "reply_trace.detail", {"identifier": bad}) == "reply_trace_invalid"
    # Exactly 100 characters is still a valid identifier: the read reaches the record
    # store, so a missing one is not_found while 101 characters is refused as invalid.
    boundary = "b" * 100
    assert denied(service, "reply_trace.detail", {"identifier": boundary}) == "not_found"
    seed(store, at=3, identifier=boundary)
    assert detail(service, boundary)["id"] == boundary
    assert denied(service, "reply_trace.detail", {}) == "management_operation"
    assert denied(service, "reply_trace.detail", {"identifier": boundary, "limit": 5}) == "management_operation"


def test_trace_reads_need_an_administrator_a_live_session_and_no_lock(pg_store):
    store = pg_store
    service = service_for(store)
    record = seed(store)

    # A request from another authority is forged, and a non-administrator one is not enough.
    forged = RequestAuthority().issue(actor=service.actor, conversation="desktop:forged", audience="private",
                                      recipients=(service.actor,), purpose="management", administrator=True,
                                      session_id=service.session_id)
    with pytest.raises(AssistantError) as error:
        trace_reads.listing(service, forged, 5)
    assert error.value.code == "access_denied"
    plain = service.authority.issue(actor=service.actor, conversation="desktop:" + service.instance,
                                    audience="private", recipients=(service.actor,), purpose="management",
                                    session_id=service.session_id)
    with pytest.raises(AssistantError) as error:
        trace_reads.detail(service, plain, record["id"])
    assert error.value.code == "access_denied"

    # Locking the app refuses both reads and returns the same code as every other entry.
    service.enable_lock("synthetic-password")
    service.lock()
    assert denied(service, "reply_trace.list", {"limit": 5}) == "desktop_locked"
    assert denied(service, "reply_trace.detail", {"identifier": record["id"]}) == "desktop_locked"

    # A request issued for the previous lock revision is retired by the next change.
    service.unlock("synthetic-password")
    stale = service.request()
    assert trace_reads.listing(service, stale, 5)["count"] == 1
    service.disable_lock("synthetic-password")
    with pytest.raises(AssistantError) as error:
        trace_reads.listing(service, stale, 5)
    assert error.value.code == "access_denied"
    assert result(service, "reply_trace.list", {"limit": 5})["count"] == 1


def snapshot(store):
    with store.db() as db:
        state = [row[0] for row in db.execute("SELECT key FROM state ORDER BY key").fetchall()]
        content = [tuple(row) for row in db.execute(
            "SELECT kind,id,version FROM content_records ORDER BY kind,id").fetchall()]
        policies = [tuple(row) for row in db.execute(
            "SELECT resource,version,valid FROM governance_policies ORDER BY resource").fetchall()]
        audit = db.execute("SELECT COUNT(*) FROM governance_audit").fetchone()[0]
    return {"state": state, "content": content, "policies": policies, "audit": audit}


def ingest(store, native="trace-one"):
    event = Event("alpha", "bot", native, ActorRef("alpha", "a"), "dm",
                  datetime.now(timezone.utc).isoformat(), "我住上海")
    events.ingest(store, event)
    GovernedContext(store, event).classify_event()
    return event


@pytest.mark.asyncio
async def test_an_auto_written_trace_is_readable_offline_and_reading_writes_nothing(pg_store):
    store = pg_store
    model, adapter = Model(), Adapter()
    core = Companion(store, model, Host(), [adapter])
    core.start()
    question = ingest(store)
    await core.handle(question)
    assert len(adapter.sent) == 1 and len(model.contexts) == 1
    service = service_for(store)

    before = snapshot(store)
    page = result(service, "reply_trace.list", {"limit": 5})
    assert page["count"] == 1
    row = page["items"][0]
    # The current database's own record is readable with the platform and model offline.
    assert row["status"] == "completed" and row["platform"] == "alpha"
    assert type(row["duration_ms"]) is int and row["source_count"] >= 1
    shown = detail(service, row["id"])
    # Sources owned by the platform sender are not readable by the local administrator:
    # they stay counted and disclosed instead of being shown or dropped silently.
    assert "event/" + question.id not in {item["resource"] for item in shown["sources"]}
    assert shown["hidden_sources"] == shown["source_count"] - len(shown["sources"])
    assert shown["hidden_sources"] >= 1
    assert {step["stage"] for step in shown["steps"]} >= {"context", "outcome"}

    # Reading is not a change: no record, policy, audit or idempotency row was written.
    assert snapshot(store) == before
    assert not [key for key in before["state"] if key.startswith("desktop-request:")]
    assert len(adapter.sent) == 1 and len(model.contexts) == 1
    assert store.state("reply-trace:" + row["id"])["status"] == "completed"
