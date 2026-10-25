from dataclasses import replace

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.storage import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "core")
    assert value.initialize()["ok"]
    return value


def incoming(native="first", account="bot", occurred="2026-10-22T04:01:00+00:00"):
    return Event("alpha", account, native, ActorRef("alpha", "100"), "room", occurred, "真实消息")


def test_pending_uses_instant_for_mixed_offsets_and_legacy_columns(store):
    item = incoming()
    receipt = events.ingest(store, item)
    # A pre-0.5.1 database may contain any legitimate ISO offset representation.
    with store.db() as db:
        db.execute("UPDATE core_events SET occurred=? WHERE id=?", ("2026-10-22T04:01:00Z", item.id))
    assert events.pending(store, [["alpha", "bot"]], "2026-10-22T12:00:00+08:00") == [item]
    assert events.get(store, item.id)["version"] == receipt["version"]
    store.set_state("core-processed:" + item.id, receipt["version"])
    assert not events.pending(store, [["alpha", "bot"]], "2026-10-22T12:00:00+08:00")


def test_corrected_event_updates_every_query_field_together(store):
    old = incoming()
    original = events.ingest(store, old)
    new = replace(old, actor=ActorRef("alpha", "200"), conversation="corrected-room",
                  occurred_at="2026-10-22T12:03:00+08:00", text="来源已更正")
    updated = events.ingest(store, new)
    assert updated["changed"] and original["version"] != updated["version"]
    assert not events.recent(store, actor=old.actor)
    assert not events.recent(store, conversation=old.thread)
    assert Event.from_dict(events.recent(store, actor=new.actor)[0]["event"]) == new
    assert Event.from_dict(events.recent(store, conversation=new.thread)[0]["event"]) == new
    assert events.pending(store, [["alpha", "bot"]], "2026-10-22T04:02:00Z") == [new]
    with store.db() as db:
        assert db.execute("SELECT occurred FROM core_events WHERE id=?", (new.id,)).fetchone()[0] == (
            "2026-10-22T04:03:00.000000+00:00")


def test_pending_is_scoped_bounded_and_requeues_changed_version(store):
    items = [incoming(str(i)) for i in range(4)]
    for item in [*items, incoming(account="other")]:
        events.ingest(store, item)
    first = events.pending(store, [["alpha", "bot"]], "2026-10-22T04:00:00Z", limit=2)
    assert len(first) == 2 and all(item.account == "bot" for item in first)
    for item in items:
        store.set_state("core-processed:" + item.id, events.get(store, item.id)["version"])
    assert not events.pending(store, [["alpha", "bot"]], "2026-10-22T04:00:00Z")
    corrected = replace(items[0], text="修改后的有效消息")
    events.ingest(store, corrected)
    assert events.pending(store, [["alpha", "bot"]], "2026-10-22T04:00:00Z") == [corrected]
    events.forget(store, corrected.id)
    assert events.ingest(store, corrected)["deleted"]
    assert not events.pending(store, [["alpha", "bot"]], "2026-10-22T04:00:00Z")


def test_recent_orders_old_offset_columns_chronologically(store):
    first = incoming("one", occurred="2026-10-22T12:00:00+08:00")
    second = incoming("two", occurred="2026-10-22T04:01:00Z")
    for item in (first, second):
        events.ingest(store, item)
        with store.db() as db:
            db.execute("UPDATE core_events SET occurred=? WHERE id=?", (item.occurred_at, item.id))
    assert [r["event"]["id"] for r in events.recent(store, actor=first.actor)] == [first.id, second.id]


@pytest.mark.parametrize("value", ["not-a-date", "2026-10-22T12:00:00", "2026-10-99T00:00:00Z"])
def test_invalid_source_time_is_rejected_before_writing(store, value):
    with pytest.raises(AssistantError) as exc:
        events.ingest(store, incoming(occurred=value))
    assert exc.value.code == "invalid_event_time"
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 0


def test_context_visibility_is_applied_before_ranking_limits_and_catalog(store):
    cap = store.read("companion")["data"]["context_record_limit"]
    def save(item_id, content, reference):
        store.write("memories", {"schema_version": 1, "id": item_id, "kind": "experience", "content": content,
            "source": {"type": "interaction", "recorded_at": "2026-10-22T04:00:00Z", "reference": reference},
            "confidence": "observed", "tags": [], "locked": False}, None, "test", item_id)
    save("own", "蓝色桌面草图采用圆角。" + "这是一条当前会话的详细设计说明。" * 50, "own-room")
    for i in range(cap + 2):
        save(f"foreign-{i}", "蓝色桌面草图设计", "other-room")
    context = store.context("蓝色桌面草图设计", references=[{"kind": "memories", "id": "foreign-0"}],
                            record_filter=lambda r: r["data"]["source"]["reference"] == "own-room")
    assert [r["data"]["id"] for r in context["records"]] == ["own"]
    assert [r["id"] for r in context["catalog"]] == ["own"]
    assert context["total_records"] == 1
