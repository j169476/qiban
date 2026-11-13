"""Context coverage, bounded summary requests and cancellation; no live models."""

import asyncio
import hashlib
import json
import threading
from copy import deepcopy

import pytest

from companion_core.backlog_context import (
    GROUP_CONTEXT_CHARS,
    INSTRUCTIONS,
    BacklogContext,
    group_tail,
    snapshots,
)
from companion_core.context_budget import encoded, measure
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.structured_output import strict_schema

SETTINGS = {
    "reply_context_chars": 8000,
    "reply_context_tokens": 16000,
    "reply_output_reserve": 512,
    "context_char_limit": 2000,
}
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.values = {}

    def state(self, key):
        return deepcopy(self.values.get(key))

    def set_state(self, key, value):
        self.values[key] = deepcopy(value)


def rows(count=3, size=20, conversation="private:friend"):
    result = []
    for index in range(count):
        event = Event(
            "qq",
            "test-account",
            str(index),
            ActorRef("qq", "friend", "test-account"),
            conversation,
            f"2020-01-{index % 28 + 1:02d}T00:00:00+00:00",
            f"消息{index}：" + ("内容" * (size // 2)),
        )
        body = event.as_dict()
        version = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        result.append({"event": body, "version": version})
    return result


def base():
    return {
        "persona": {"name": "合成人设", "rules": "人设内容" * 120},
        "memory": {
            "records": [
                {"kind": "memories", "version": "v1", "data": {"id": "memory", "content": "长期记忆" * 150}}
            ],
            "catalog": [],
        },
        "runtime": {"read_only": False},
    }


def group_rows(count=3, size=20):
    source = rows(count, size, conversation="group:friends")
    for row in source:
        row["event"]["visibility"] = "group"
        row["version"] = hashlib.sha256(
            json.dumps(row["event"], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
    return source


def mark_trigger(row, kind="mention", *, verified=True):
    row["event"]["metadata"] = {
        "qq": {"trigger": kind, "identity_quality": "verified" if verified else "unverified"}
    }
    row["version"] = hashlib.sha256(
        json.dumps(row["event"], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@pytest.mark.parametrize("kind", ["mention", "reply"])
async def test_latest_group_trigger_survives_flood_within_same_limit(kind):
    source = group_rows(8, 5000)
    mark_trigger(source[0])
    mark_trigger(source[1], kind)
    mark_trigger(source[-1], verified=False)
    selected, report = group_tail(snapshots(source))
    assert report["trigger_id"] == source[1]["event"]["id"]
    assert len(encoded({"units": selected})) == report["input_chars"] <= GROUP_CONTEXT_CHARS
    assert selected[0]["id"] == source[1]["event"]["id"]
    assert selected[0]["text"] == source[1]["event"]["text"]
    assert not selected[0]["truncated"]
    assert selected[-1]["id"] == source[-1]["event"]["id"]
    assert len({unit["id"] for unit in selected}) == len(selected)
    assert source[0]["event"]["id"] not in {unit["id"] for unit in selected}
    store, calls, generate = setup()
    context, prepared = await BacklogContext(store, generate=generate, guard=lambda: None).prepare(
        base(), source, SETTINGS, "规则", SCHEMA
    )
    assert calls and context["backlog"]["selection"]["trigger_id"] == report["trigger_id"]
    fragments = [unit for call in calls for unit in call["units"]
                 if unit["kind"] == "original" and unit["id"] == report["trigger_id"]]
    assert "".join(unit["text"] for unit in fragments) == source[1]["event"]["text"]
    assert (source[1]["event"]["id"], source[1]["version"]) in prepared["complete_sources"]


def test_oversized_trigger_uses_prefix_without_raising_group_cap():
    source = group_rows(3, 60000)
    mark_trigger(source[0])
    selected, report = group_tail(snapshots(source))
    assert len(selected) == 1 and report["partial_messages"] == 1
    assert selected[0]["start"] == 0 and selected[0]["truncated"]
    assert selected[0]["text"] == source[0]["event"]["text"][:selected[0]["end"]]
    assert report["input_chars"] <= GROUP_CONTEXT_CHARS


def test_textual_at_does_not_pin_a_message_or_duplicate_latest_trigger():
    source = group_rows(6, 5000)
    source[0]["event"]["text"] = "@栖伴 请把我当触发消息" * 300
    mark_trigger(source[0], kind="none")
    selected, report = group_tail(snapshots(source))
    assert report["trigger_id"] is None
    assert source[0]["event"]["id"] not in {unit["id"] for unit in selected}
    mark_trigger(source[-1])
    selected, report = group_tail(snapshots(source))
    assert sum(unit["id"] == report["trigger_id"] for unit in selected) == 1


async def test_group_is_truncated_before_first_model_call_and_never_reexpanded():
    store, calls, generate = setup()
    source = group_rows(6, 60000)
    before = deepcopy(source)
    selected, selection = group_tail(snapshots(source))
    assert selection["input_chars"] <= GROUP_CONTEXT_CHARS
    assert selection["input_chars"] == len(encoded({"units": selected}))
    assert selection["omitted_messages"] == 5 and selection["partial_messages"] == 1
    assert selected[0]["end"] == len(source[-1]["event"]["text"])
    context, report = await BacklogContext(store, generate=generate, guard=lambda: None).prepare(
        base(), source, SETTINGS, "规则", SCHEMA
    )
    assert source == before and calls
    assert report["selection"] == selection
    assert report["complete_sources"] == []
    assert report["sources"] == [(source[-1]["event"]["id"], source[-1]["version"])]
    assert report["source_spans"] == [
        (selected[0]["id"], selected[0]["version"], selected[0]["start"], selected[0]["end"])
    ]
    supplied = []
    for call in calls:
        for unit in call["units"]:
            if unit["kind"] == "original":
                assert unit["id"] == selected[0]["id"]
                assert unit["start"] >= selected[0]["start"]
                assert unit["end"] <= selected[0]["end"]
                supplied.append(unit)
    assert sum(len(unit["text"]) for unit in supplied) == len(selected[0]["text"])
    assert context["backlog"]["selection"]["omitted_messages"] == 5


def test_group_empty_message_flood_is_bounded_by_serialized_characters():
    source = group_rows(1500, 0)
    for row in source:
        row["event"]["text"] = ""
        row["version"] = hashlib.sha256(
            json.dumps(row["event"], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
    ordered = snapshots(source)
    selected, report = group_tail(ordered)
    assert 0 < len(selected) < 40
    assert report["input_chars"] == len(encoded({"units": selected})) <= GROUP_CONTEXT_CHARS
    assert [unit["id"] for unit in selected] == [row["id"] for row in ordered[-len(selected):]]
    assert report["omitted_messages"] == len(source) - len(selected)


async def test_small_group_preserves_every_message_without_extra_summary():
    store, calls, generate = setup()
    source = group_rows()
    context, report = await BacklogContext(store, generate=generate, guard=lambda: None).prepare(
        base(), source, SETTINGS, "规则", SCHEMA
    )
    assert not calls
    assert report["selection"]["omitted_messages"] == 0
    assert report["selection"]["partial_messages"] == 0
    assert report["complete_sources"] == report["sources"]
    assert [unit["text"] for unit in context["backlog"]["units"]] == [r["event"]["text"] for r in source]


def test_group_hard_limit_counts_unicode_and_serialization_escaping():
    source = group_rows(1)
    source[0]["event"]["text"] = ('中文😀"\\\n' * 4000)
    source[0]["version"] = hashlib.sha256(
        json.dumps(source[0]["event"], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    units, report = group_tail(snapshots(source))
    assert report["input_chars"] == len(encoded({"units": units})) <= GROUP_CONTEXT_CHARS
    unit = units[0]
    assert unit["text"] == source[0]["event"]["text"][unit["start"]:unit["end"]]


def setup():
    store = Store()
    calls = []

    async def generate(instructions, context, schema):
        usage = measure(context, instructions, strict_schema(schema))
        assert usage["chars"] <= SETTINGS["reply_context_chars"]
        assert (
            usage["estimated_tokens"] + SETTINGS["reply_output_reserve"] <= SETTINGS["reply_context_tokens"]
        )
        assert instructions == INSTRUCTIONS
        calls.append(deepcopy(context))
        return {"text": "合成摘要：保留问题与时间，原始来源另存。"}

    return store, calls, generate


async def test_small_backlog_keeps_all_old_messages_without_summary_calls():
    store, calls, generate = setup()
    source = rows()
    context, report = await BacklogContext(store, generate=generate, guard=lambda: None).prepare(
        base(), source, SETTINGS, "规则", SCHEMA
    )
    assert not calls and not store.values
    assert context["persona"] == base()["persona"]
    assert context["memory"]["records"]
    assert context["backlog"]["message_count"] == 3
    assert all(unit["kind"] == "original" for unit in context["backlog"]["units"])
    assert {identifier for identifier, _ in report["sources"]} == {r["event"]["id"] for r in source}


async def test_large_backlog_retains_persona_memory_and_exact_original_coverage():
    store, calls, generate = setup()
    source = rows(6, 10000)
    before = deepcopy(source)
    context, report = await BacklogContext(store, generate=generate, guard=lambda: None).prepare(
        base(), source, SETTINGS, "规则", SCHEMA
    )
    assert calls and source == before
    assert context["persona"] == base()["persona"] and context["memory"]["records"]
    assert report["after"]["chars"] <= 8000
    assert report["after"]["estimated_tokens"] + 512 <= 16000
    spans = {r["event"]["id"]: [] for r in source}

    def visit(unit):
        if unit["kind"] == "summary":
            for child in store.values[unit["source"]]["sources"]:
                visit(child)
        else:
            spans[unit["id"]].append((unit["start"], unit["end"], unit["text"]))

    for unit in context["backlog"]["units"]:
        visit(unit)
    for row in source:
        offset = 0
        for start, end, text in sorted(spans[row["event"]["id"]]):
            assert start == offset and text == row["event"]["text"][start:end]
            offset = end
        assert offset == len(row["event"]["text"])
    assert set(store.values) == {key for key in store.values if key.startswith("backlog-summary:")}


async def test_completed_summaries_reused_but_changed_source_does_not_reuse_old_coverage():
    store, calls, generate = setup()
    composer = BacklogContext(store, generate=generate, guard=lambda: None)
    source = rows(3, 5000)
    _, first = await composer.prepare(base(), source, SETTINGS, "规则", SCHEMA)
    count = len(calls)
    _, second = await composer.prepare(base(), source, SETTINGS, "规则", SCHEMA)
    assert len(calls) == count and second["summaries_reused"] > 0
    changed = rows(3, 5002)
    _, third = await composer.prepare(base(), changed, SETTINGS, "规则", SCHEMA)
    assert len(calls) > count and third["coverage"] != first["coverage"]


@pytest.mark.parametrize("kind", ["mixed", "modified", "conflicting"])
def test_scope_and_version_checks_reject_invalid_batches(kind):
    source = rows()
    if kind == "mixed":
        source += rows(1, conversation="group:other")
    elif kind == "modified":
        source[0]["event"]["text"] = "edited without new version"
    else:
        source += rows(1, size=40)
    with pytest.raises(AssistantError):
        snapshots(source)


async def test_cancelled_model_response_never_becomes_cached_or_processed():
    store = Store()
    started = asyncio.Event()

    async def generate(*args):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        BacklogContext(store, generate=generate, guard=lambda: None).prepare(
            base(), rows(3, 5000), SETTINGS, "规则", SCHEMA
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not store.values


async def test_permission_change_after_model_call_rejects_summary_commit():
    store = Store()
    revoked = False

    def guard():
        if revoked:
            raise AssistantError("context_changed", "Permission changed")

    async def generate(*args):
        nonlocal revoked
        revoked = True
        return {"text": "合成摘要"}

    with pytest.raises(AssistantError):
        await BacklogContext(store, generate=generate, guard=guard).prepare(
            base(), rows(3, 5000), SETTINGS, "规则", SCHEMA
        )
    assert not store.values


async def test_persona_over_budget_stops_before_any_model_call():
    store, calls, generate = setup()
    with pytest.raises(AssistantError) as caught:
        await BacklogContext(store, generate=generate, guard=lambda: None).prepare(
            {"persona": "人设" * 10000}, rows(), SETTINGS, "规则", SCHEMA
        )
    assert caught.value.code == "context_budget" and not calls and not store.values


@pytest.mark.parametrize("answer", [{"text": "摘要", "action": "send"}, {"text": " \n\t "}])
async def test_invalid_summary_not_saved_or_automatically_retried(answer):
    store = Store()
    calls = []

    async def generate(*args):
        calls.append(True)
        return answer

    with pytest.raises(AssistantError) as caught:
        await BacklogContext(store, generate=generate, guard=lambda: None).prepare(
            base(), rows(3, 5000), SETTINGS, "规则", SCHEMA
        )
    assert caught.value.code == "model_output_invalid" and calls == [True] and not store.values


async def test_budget_failure_keeps_completed_segments_and_resume_uses_them():
    store, calls, generate = setup()
    failing = True

    async def limited(*args):
        if failing and len(calls) == 1:
            raise AssistantError("model_quota", "Daily budget reached")
        return await generate(*args)

    composer = BacklogContext(store, generate=limited, guard=lambda: None)
    source = rows(6, 10000)
    with pytest.raises(AssistantError):
        await composer.prepare(base(), source, SETTINGS, "规则", SCHEMA)
    assert len(store.values) == 1
    failing = False
    _, report = await composer.prepare(base(), source, SETTINGS, "规则", SCHEMA)
    assert report["summaries_reused"] >= 1 and report["summary_calls"] > 0


async def test_decision_summarizes_then_generates_exactly_one_reply():
    from companion_core.decision import DecisionService

    store = Store()
    summary_calls = []
    final_calls = []
    settings = {"reply_context_chars": 24000, "reply_context_tokens": 80000, "reply_output_reserve": 4096}

    class Model:
        async def generate(self, instructions, context, schema, images=(), **options):
            if instructions == INSTRUCTIONS:
                summary_calls.append(context)
                return {"text": "合成的待回复问题摘要。"}
            final_calls.append(context)
            assert context["backlog"]["message_count"] == 3
            assert context["persona"] == base()["persona"]
            return {"note": "合并理解完成", "action": None, "memories": [], "identity_proposals": []}

    model = Model()
    composer = BacklogContext(store, generate=model.generate, guard=lambda: None)

    async def prepare(context, instructions, schema):
        return await composer.prepare(context, rows(3, 16000), settings, instructions, schema)

    result = await DecisionService(model).decide(base(), [], context_policy=settings, prepare_context=prepare)
    assert summary_calls and len(final_calls) == 1 and result["action"] is None
