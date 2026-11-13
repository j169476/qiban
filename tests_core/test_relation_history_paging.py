"""Relationship-claim adjudication history: ordering, boundaries, immutability.

The history lives in the existing encrypted ``state`` table, so these cases run on
the ordinary file store (the same SQL shape). They pin the boundary arithmetic a
one-row page needs: comparing against ``prefix + cursor`` would re-read the entry
that was just returned, because every key carries ``:<version>`` after the
sequence. Nothing here touches a real PostgreSQL database.
"""
import pytest

from companion_core import relation_history
from companion_core.errors import AssistantError
from companion_core.storage import Store

CLAIM = "a" * 32


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "core")
    assert value.initialize()["ok"]
    return value


def add(store, status, sequence_version="v1", *, claim=CLAIM, reason="合成说明", before="claimed"):
    with store.lock, store.db() as db:
        return relation_history.append(
            store, db=db, claim=claim,
            before={"status": before, "version": "old", "policy_version": "p0",
                    "valid": before != "revoked"},
            after={"status": status, "version": sequence_version, "policy_version": sequence_version,
                   "valid": status != "revoked"},
            actor="actor-synthetic", at=1800000000.0, reason=reason,
            snapshot={"subject": "s", "object": "o", "declarer": "d", "status": before,
                      "version": "old", "body": {"relation": "同事", "evidence": ["event/e"]}},
            sources=(("event/e", "ev1"),))


def walk(store, limit):
    after, seen = "", []
    for _ in range(50):
        page = relation_history.page(store, CLAIM, after, limit)
        seen.extend(item["cursor"] for item in page["items"])
        if not page["next"]:
            return seen
        after = page["next"]
    raise AssertionError("paging did not terminate")


def test_one_row_pages_are_disjoint_and_complete(store):
    for index in range(4):
        add(store, "confirmed", "v%d" % (index + 1))
    seen = walk(store, 1)
    assert seen == ["%012d" % index for index in range(1, 5)]
    assert len(set(seen)) == len(seen)
    sequences = []
    after = ""
    while True:
        page = relation_history.page(store, CLAIM, after, 3)
        sequences.extend(item["sequence"] for item in page["items"])
        if not page["next"]:
            break
        after = page["next"]
    assert sequences == [1, 2, 3, 4]


def test_a_short_numeric_cursor_means_the_same_as_the_padded_one(store):
    for index in range(3):
        add(store, "confirmed", "v%d" % (index + 1))
    assert relation_history.boundary(CLAIM, "2") == relation_history.boundary(CLAIM, "000000000002")
    padded = relation_history.page(store, CLAIM, "000000000001", 5)
    short = relation_history.page(store, CLAIM, "1", 5)
    assert [item["sequence"] for item in padded["items"]] == [2, 3]
    assert [item["sequence"] for item in short["items"]] == [2, 3]
    # A cursor past the end is not an error and yields nothing more.
    assert relation_history.page(store, CLAIM, "000000000009", 5)["items"] == []
    # The largest representable cursor can never restart the first page.
    assert relation_history.page(store, CLAIM, "999999999999", 1) == {
        "items": [], "next": None, "count": 0, "limit": 1, "after": "999999999999",
        "page_max": relation_history.PAGE_MAX}


def test_cursors_and_limits_are_validated_consistently(store):
    add(store, "confirmed", "v1")
    for after in ("x", "1 ", "-1", "1234567890123", "１"):
        with pytest.raises(AssistantError) as caught:
            relation_history.page(store, CLAIM, after, 5)
        assert caught.value.code == "relation_history"
    for limit in (0, relation_history.PAGE_MAX + 1, True, 1.5, "1"):
        with pytest.raises(AssistantError) as caught:
            relation_history.page(store, CLAIM, "", limit)
        assert caught.value.code == "relation_history"
    with pytest.raises(AssistantError) as caught:
        relation_history.page(store, "bad/id", "", 5)
    assert caught.value.code == "relation_claim"


def test_entries_are_immutable_and_bounds_are_exact(store):
    assert relation_history.bounds(store, CLAIM) == {"count": 0, "first": None, "last": None}
    add(store, "confirmed", "v1", reason="第一次")
    first = relation_history.page(store, CLAIM, "", 5)["items"]
    add(store, "disputed", "v2", reason="第二次")
    add(store, "revoked", "v3", reason="第三次")
    assert relation_history.page(store, CLAIM, "", 5)["items"][:1] == first
    summary = relation_history.bounds(store, CLAIM)
    assert summary["count"] == 3
    assert summary["first"]["reason"] == "第一次"
    assert summary["last"]["reason"] == "第三次"
    assert summary["last"]["after"]["status"] == "revoked"
    assert summary["last"]["restored"] is False
    # A restore is recorded as such, and each claim keeps its own sequence.
    restored = add(store, "confirmed", "v4", reason="恢复", before="revoked")
    assert restored["restored"] is True
    other = add(store, "disputed", "w1", claim="b" * 32)
    assert other["sequence"] == 1
    assert relation_history.bounds(store, "b" * 32)["count"] == 1
    assert relation_history.page(store, CLAIM, "", 5)["count"] == 4


def test_a_damaged_entry_is_reported_not_skipped(store):
    add(store, "confirmed", "v1")
    add(store, "disputed", "v2")
    with store.db() as db:
        db.execute("UPDATE state SET value=? WHERE key LIKE ?",
                   ("enc1:not-a-payload", relation_history.PREFIX + CLAIM + ":%"))
    page = relation_history.page(store, CLAIM, "", 5)
    assert page["count"] == 2
    assert all(item["unreadable"] is True for item in page["items"])
    assert all(item["detail"] for item in page["items"])
    summary = relation_history.bounds(store, CLAIM)
    assert summary["count"] == 2 and summary["first"]["unreadable"] is True


def test_a_reason_over_the_bound_is_refused(store):
    with pytest.raises(AssistantError) as caught:
        add(store, "confirmed", reason="字" * (relation_history.REASON_MAX + 1))
    assert caught.value.code == "relation_reason"
    assert relation_history.bounds(store, CLAIM)["count"] == 0
