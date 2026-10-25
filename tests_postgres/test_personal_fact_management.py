from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.fact_management import FactManagement
from companion_core.governance import AdminService, GovernedContext
from tests_postgres.test_fact_trace_governance import facts, seed


def management(store):
    admin = AdminService(store)
    owner = ActorRef("local", "owner")
    request = admin.authority.issue(actor=owner, conversation="admin", audience="private", recipients=(owner,),
                                    purpose="management", administrator=True)
    return FactManagement(admin), request


def seed_chain(store, count=205):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    prior = None
    with store.lock, store.db():
        for index in range(count):
            event = Event("alpha", "bot", f"history-{index}", ActorRef("alpha", "a"), "dm",
                          (start + timedelta(hours=index)).isoformat(), f"我住城市{index}")
            events.ingest(store, event)
            governed = GovernedContext(store, event)
            governed.classify_event()
            identifier = f"fact-history-{index:04}"
            store.write("memories", {"schema_version": 1, "id": identifier, "kind": "user_preference",
                "content": event.text, "confidence": "explicit", "tags": ["personal_fact"], "locked": False,
                "source": {"type": "interaction", "recorded_at": event.occurred_at, "reference": event.id},
                "fact": {"actor": event.actor.key, "account": event.account, "conversation": event.thread,
                         "attribute": "residence", "value": f"城市{index}", "quote": event.text,
                         "valid_from": event.occurred_at, "supersedes": [prior] if prior else []}}, None, "synthetic", identifier)
            governed.classify_written("memories", identifier, personal=True,
                                     sources=["event/" + event.id, *(["memories/" + prior] if prior else [])])
            prior = identifier
    return prior


def test_more_than_200_facts_keep_current_view_bounded_and_history_pageable(pg_store, monkeypatch):
    latest = seed_chain(pg_store)
    reads = []
    original = pg_store._decode

    def decode(kind, identifier, *args):
        if kind == "memories":
            reads.append(identifier)
        return original(kind, identifier, *args)

    monkeypatch.setattr(pg_store, "_decode", decode)
    _, governed, value = facts(pg_store, "read", "现在住所")
    material = value.context()
    assert material["available"] and material["counts"] == {"active": 1, "disputed": 0, "future": 0, "historical": 204}
    assert any(item["id"] == latest and item["status"] == "active" for item in material["items"])
    assert len(material["items"]) <= 32 and material["truncated"]
    assert len(reads) <= 128  # two explicit context() calls; never decrypt all 205 history bodies
    governed.check()
    new = facts(pg_store, "new", "我搬到新城")[2]
    assert new.save([{"attribute": "residence", "value": "新城", "quote": "我搬到新城",
                      "valid_from": None, "supersedes": [latest]}])["written"] == 1
    manager, admin = management(pg_store)
    page = manager.history(admin, latest, limit=10)
    second = manager.history(admin, latest, limit=10, after=page["next"])
    assert page["total"] == 206 and not ({x["id"] for x in page["items"]} & {x["id"] for x in second["items"]})


def test_confirmation_appends_a_fact_preserves_sources_and_rejects_stale_revision(pg_store):
    seed(pg_store)
    second = facts(pg_store, "two", "我住杭州")[2]
    second.save([{"attribute": "residence", "value": "杭州", "quote": "我住杭州", "valid_from": None, "supersedes": []}])
    manager, admin = management(pg_store)
    rows = facts(pg_store, "read", "住所")[2].context()["items"]
    chosen = next(row for row in rows if row["value"] == "杭州")
    before = {row["id"]: pg_store.version("memories", row["id"]) for row in rows}
    timeline = manager.history(admin, chosen["id"])
    result = manager.confirm(admin, chosen["id"], timeline["revision"], "用户已核实当前住址", list(before))
    assert all(pg_store.version("memories", key) == version for key, version in before.items())
    view = facts(pg_store, "after", "住所")[2].context()
    assert [(row["value"], row["status"]) for row in view["items"] if row["status"] == "active"] == [("杭州", "active")]
    assert pg_store.read("memories", result["id"])["data"]["fact"]["confirmation"]["by"] == admin.actor
    with pytest.raises(AssistantError, match="变化"):
        manager.confirm(admin, chosen["id"], timeline["revision"], "重复提交", list(before))
    source_policy = manager.admin.repository.policy("memories/" + chosen["id"])
    manager.admin.repository.set_policy(replace(source_policy, valid=False, version="revoked"), source_policy.version, "test")
    assert result["id"] not in {row["id"] for row in facts(pg_store, "revoked", "住所")[2].context()["items"]}


def test_history_and_confirmation_require_admin_and_refuse_unreadable_sources(pg_store):
    event, governed = seed(pg_store)
    identifier = facts(pg_store, "read", "住所")[2].context()["items"][0]["id"]
    manager, admin = management(pg_store)
    actor = ActorRef("alpha", "a")
    role = manager.admin.authority.issue(actor=actor, conversation=event.thread, audience="private", recipients=(actor,))
    with pytest.raises(AssistantError):
        manager.history(role, identifier)
    policy = governed.repository.policy("event/" + event.id)
    governed.repository.set_policy(replace(policy, valid=False, version="revoked"), policy.version, "test")
    with pytest.raises(AssistantError):
        manager.history(admin, identifier)


def test_index_scope_and_values_are_opaque_and_backfill_is_idempotent(pg_store):
    seed(pg_store)
    from companion_core.fact_index import backfill

    with pg_store.db() as db:
        rows = db.execute("SELECT * FROM personal_fact_index").fetchall()
        assert len(rows) == 1 and "上海" not in str(rows) and rows[0]["scope"] != "dm"
        db.execute("DELETE FROM personal_fact_index")
    backfill(pg_store)
    backfill(pg_store)
    assert len(facts(pg_store, "read", "住所")[2].context()["items"]) == 1
    assert not facts(pg_store, "other", "住所", user="someone-else")[2].context()["items"]


def test_encrypted_backup_restores_fact_index_and_full_projection_sources(pg_store):
    from companion_core.postgres_transfer import create, fingerprint, restore

    seed(pg_store)
    _, original_reading, original = facts(pg_store, "before-backup", "住所")
    before = original.context()
    projection = {key for key in original_reading.resources if key.startswith("personal-fact-view/")}
    blob = create(pg_store, pg_store._backup_key)
    later = facts(pg_store, "after-backup", "我搬到杭州")[2]
    assert later.save([{"attribute": "residence", "value": "杭州", "quote": "我搬到杭州",
                        "valid_from": None, "supersedes": [before["items"][0]["id"]]}])["written"] == 1
    restore(pg_store, blob, pg_store._backup_key, expected=fingerprint(pg_store))
    _, restored_reading, restored = facts(pg_store, "after-restore", "住所")
    assert restored.context()["items"] == before["items"]
    assert projection <= restored_reading.resources
    restored_reading.freeze().check()
    with pg_store.db() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM personal_fact_index").fetchone()["n"] == 1


def test_future_assertion_cannot_be_confirmed_into_effect_early(pg_store):
    seed(pg_store)
    value = facts(pg_store, "future", "我将在2099-12-01搬到杭州")[2]
    old = value.context()["items"][0]["id"]
    value.save([{"attribute": "residence", "value": "杭州", "quote": value.event.text,
                 "valid_from": "2099-12-01", "supersedes": [old]}])
    future = next(row for row in facts(pg_store, "read", "住所")[2].context()["items"] if row["status"] == "future")
    manager, admin = management(pg_store)
    page = manager.history(admin, future["id"])
    with pytest.raises(AssistantError, match="未来"):
        manager.confirm(admin, future["id"], page["revision"], "不能提前", [old, future["id"]])


def test_fact_timeline_web_entry_is_admin_only_and_requires_csrf(pg_store):
    from companion_service.service import Service
    from tests_postgres.test_runtime import Model
    from tests_postgres.test_web import PASSWORD, Host, client, login

    service = Service(pg_store, Model(), Host(), bridge_secret="synthetic-bridge-secret")
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    seed(pg_store)
    identifier = facts(pg_store, "read", "住所")[2].context()["items"][0]["id"]
    admin, role = client(service, "admin"), client(service, "role")
    headers = login(admin, service, "admin")
    role_headers = login(role, service, "role")
    body = {"operation": "personal_fact_history", "arguments": {"id": identifier}}
    assert admin.post("/operate", json=body, headers={"origin": headers["origin"]}).status_code == 403
    assert admin.post("/operate", json=body, headers=headers).json()["total"] == 1
    assert role.post("/operate", json=body, headers=role_headers).status_code in {403, 404}
    assert 'id="fact-timeline"' in admin.get("/").text
    assert 'id="fact-timeline"' not in role.get("/").text
    assert service.status()["status"] == "paused"


def test_omitted_fact_bodies_remain_in_the_verified_source_graph(pg_store):
    # Independent conflicting assertions, so the last fact does not itself cite the first.
    for index in range(36):
        event = Event("alpha", "bot", f"independent-{index}", ActorRef("alpha", "a"), "dm",
                      f"2026-01-{index % 28 + 1:02}T00:00:00+00:00", f"我住城市{index}")
        events.ingest(pg_store, event)
        governed = GovernedContext(pg_store, event)
        governed.classify_event()
        identifier = f"fact-independent-{index:03}"
        pg_store.write("memories", {"schema_version": 1, "id": identifier, "kind": "user_preference",
            "content": event.text, "confidence": "explicit", "tags": [], "locked": False,
            "source": {"type": "interaction", "recorded_at": event.occurred_at, "reference": event.id},
            "fact": {"actor": event.actor.key, "account": event.account, "conversation": event.thread,
                     "attribute": "residence", "value": f"城市{index}", "quote": event.text,
                     "valid_from": event.occurred_at, "supersedes": []}}, None, "synthetic", identifier)
        governed.classify_written("memories", identifier, personal=True, sources=["event/" + event.id])
    _, reading, value = facts(pg_store, "question", "现在住所")
    shown = value.visible
    omitted = next(row["id"] for row in value.headers if row["id"] not in shown)
    assert "memories/" + omitted in dict(reading.lease.versions)
    policy = reading.repository.policy("memories/" + omitted)
    reading.repository.set_policy(replace(policy, valid=False, version="revoked-hidden-fact"), policy.version, "test")
    with pytest.raises(AssistantError):
        reading.check()
