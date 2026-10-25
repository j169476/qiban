from datetime import datetime, timezone

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository, GovernedContext
from companion_core.storage import Store
from companion_core.temporal_facts import PersonalFacts, project

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "facts")
    store.initialize()
    GovernanceRepository(store).initialize()
    store.set_state("governance-enabled", True)
    return store


def context(store, text, native="one", *, account="bot", user="user", conversation="dm"):
    event = Event("wechat", account, native, ActorRef("wechat", user), conversation, NOW.isoformat(), text)
    events.ingest(store, event)
    governed = GovernedContext(store, event)
    governed.classify_event()
    facts = PersonalFacts(governed, event, lambda: NOW)
    facts.context()
    governed.freeze()
    return facts


def update(text, value, *, supersedes=(), date=None, attribute="residence"):
    return {"attribute": attribute, "value": value, "quote": text, "valid_from": date,
            "supersedes": list(supersedes)}


def test_explicit_change_preserves_old_fact_and_answer_time(store):
    first = context(store, "我住上海")
    assert first.save([update("我住上海", "上海")])["written"] == 1
    later = context(store, "我已经搬到杭州", "two")
    old = later.context()["items"][0]["id"]
    assert later.save([update("我已经搬到杭州", "杭州", supersedes=[old])])["written"] == 1
    final = context(store, "现在的住所", "three")
    assert {(row["value"], row["status"]) for row in final.context()["items"]} == {
        ("上海", "historical"), ("杭州", "active")}
    assert store.read("memories", old)["data"]["content"] == "我住上海"


def test_newer_statement_without_correction_stays_disputed(store):
    context(store, "我住上海").save([update("我住上海", "上海")])
    context(store, "我住杭州", "two").save([update("我住杭州", "杭州")])
    facts = context(store, "住所", "three").context()["items"]
    assert {item["status"] for item in facts} == {"disputed"}


@pytest.mark.parametrize("text,value", [
    ("朋友说我住上海", "上海"), ("如果我住上海", "上海"), ("我住上海吗？", "上海"),
    ("我以前住上海", "上海"), ("我的朋友住上海", "上海"),
    ("我不喜欢甜咖啡", "甜咖啡"), ("我住上海", "杭州"),
    ("我明年搬到杭州", "杭州"), ("My friend lives in Shanghai", "Shanghai"),
])
def test_quotation_hypothesis_question_past_and_negation_are_not_promoted(store, text, value):
    facts = context(store, text)
    assert facts.save([update(text, value)]) == {"written": 0, "rejected": 1}
    assert not store.records("memories")[0]


def test_account_actor_and_conversation_are_separate(store):
    context(store, "我住上海").save([update("我住上海", "上海")])
    for scope in ({"account": "other"}, {"user": "other"}, {"conversation": "other"}):
        assert not context(store, "我住杭州", "two", **scope).context()["items"]


def test_non_explicit_replacement_and_unknown_target_rejected(store):
    context(store, "我住上海").save([update("我住上海", "上海")])
    facts = context(store, "我住杭州", "two")
    old = facts.context()["items"][0]["id"]
    assert facts.save([update("我住杭州", "杭州", supersedes=[old])])["rejected"] == 1
    facts = context(store, "我搬到杭州", "three")
    assert facts.save([update("我搬到杭州", "杭州", supersedes=["unknown"])])["rejected"] == 1


def test_future_change_uses_explicit_date_and_does_not_expire_current_early(store):
    context(store, "我住上海").save([update("我住上海", "上海")])
    facts = context(store, "我将在2026-12-01搬到杭州", "two")
    old = facts.context()["items"][0]["id"]
    assert facts.save([update(facts.event.text, "杭州", supersedes=[old], date="2026-12-01")])["written"] == 1
    rows = context(store, "住所", "three").rows
    assert {(item["value"], item["status"]) for item in project(rows, NOW)} == {
        ("上海", "active"), ("杭州", "future")}
    assert {(item["value"], item["status"]) for item in project(rows, datetime(2027, 1, 1, tzinfo=timezone.utc))} == {
        ("上海", "historical"), ("杭州", "active")}


def test_new_fact_after_context_build_is_detected(store):
    reading = context(store, "住所", "question")
    context(store, "我住上海").save([update("我住上海", "上海")])
    with pytest.raises(AssistantError, match="变化"):
        reading.check()


def test_repeated_historical_value_is_a_new_dispute_not_silently_discarded(store):
    context(store, "我住上海").save([update("我住上海", "上海")])
    later = context(store, "我搬到杭州", "two")
    old = later.context()["items"][0]["id"]
    later.save([update("我搬到杭州", "杭州", supersedes=[old])])
    again = context(store, "我住上海", "three")
    assert again.save([update("我住上海", "上海")])["written"] == 1
    assert [item["status"] for item in context(store, "住所", "four").context()["items"]].count("disputed") == 2


def test_another_persons_clause_cannot_be_promoted_from_a_first_person_message(store):
    facts = context(store, "我住上海。小王住杭州。")
    assert facts.save([update("小王住杭州", "杭州")]) == {"written": 0, "rejected": 1}


@pytest.mark.parametrize("text,quote", [
    ("我以前住上海，现在住杭州", "现在住杭州"),
    ("我以前住上海，我现在住杭州。", "我现在住杭州"),
    ("其实我现在住杭州", "其实我现在住杭州"),
])
def test_complete_current_clause_can_correct_an_explicit_past_statement(store, text, quote):
    context(store, "我住上海").save([update("我住上海", "上海")])
    facts = context(store, text, "correct")
    old = facts.context()["items"][0]["id"]
    assert facts.save([update(quote, "杭州", supersedes=[old])])["written"] == 1
    assert {(r["value"], r["status"]) for r in context(store, "住所", "read").context()["items"]} == {
        ("上海", "historical"), ("杭州", "active")}


@pytest.mark.parametrize("text,quote", [
    ("如果我以前住上海，现在住杭州", "现在住杭州"),
    ("他说我以前住上海，现在住杭州", "现在住杭州"),
    ("我以前住上海，现在小王住杭州", "小王住杭州"),
    ("我以前住上海，现在住杭州吗？", "现在住杭州"),
    ('他说“我住杭州”。', "我住杭州"),
    ("我住杭州只是一个假设", "我住杭州"),
    ("我不喜欢甜咖啡", "喜欢甜咖啡"),
])
def test_clause_extraction_does_not_strip_subject_hypothesis_question_or_negation(store, text, quote):
    facts = context(store, text)
    assert facts.save([update(quote, "杭州" if "杭州" in text else "甜咖啡")])["rejected"] == 1


def test_model_cannot_supersede_a_fact_removed_by_final_context_budget(store):
    context(store, "我住上海").save([update("我住上海", "上海")])
    facts = context(store, "我搬到杭州", "two")
    old = facts.context()["items"][0]["id"]
    facts.supplied({"personal_facts": {"items": []}})
    assert facts.save([update("我搬到杭州", "杭州", supersedes=[old])])["rejected"] == 1
