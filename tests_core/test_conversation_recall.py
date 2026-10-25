import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from companion_core import conversations
from companion_core.conversation_recall import size
from companion_core.errors import AssistantError


def turn(index, content, *, account="bot", thread="alice", speaker="user", status="received"):
    stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat()
    data = {"id": f"turn-{account}-{thread}-{index}", "account_uid": account, "channel": "local",
            "thread_key": thread, "thread_id": conversations.thread_id(account, "local", thread),
            "content": content, "speaker": speaker, "sender_uid": thread if speaker == "user" else account,
            "status": status, "occurred_at": stamp, "message_id": f"message-{index}", "reply_to": "",
            "part": 0, "sequence": index, "source": {"recorded_at": stamp, "reference": f"message-{index}"}}
    return {"data": data, "version": hashlib.sha256(json.dumps(data).encode()).hexdigest()}


class Archive:
    def __init__(self, turns, *, summarize=False, **settings):
        self.turns = turns
        self.settings = {"conversation_context_turns": 4, "conversation_context_chars": 12000, **settings}
        self.summaries = []
        if summarize:
            self.summaries = [{"data": {"id": "summary", "account_uid": "bot",
                "thread_id": conversations.thread_id("bot", "local", "alice"), "content": "日常闲聊，省略了具体约定。",
                "turn_refs": [{"id": item["data"]["id"], "version": item["version"]} for item in turns[:40]],
                "previous": None}, "version": "summary-v1"}]

    def read(self, kind, identifier=None):
        if kind == "companion":
            return {"data": self.settings}
        for item in self.summaries:
            if item["data"]["id"] == identifier:
                return item
        raise AssistantError("not_found", "synthetic")

    def records(self, kind):
        return (self.turns if kind == "conversation_turns" else self.summaries), []

    def context(self, query="", queries=(), **kwargs):
        return conversations.context(self, "bot", conversations.thread_id("bot", "local", "alice"),
                                     query=query, queries=queries, **kwargs)


def archive(**settings):
    items = [turn(i, f"今天看看天气。记录 {i}") for i in range(60)]
    items[5] = turn(5, "北岸灯塔的碰面暗号是钴蓝纸鹤，约在周四。")
    items[6] = turn(6, "更正刚才的约定：改为周五，周四取消。")
    return Archive(items, **settings)


def test_detail_omitted_by_summary_is_recalled_with_adjacent_correction():
    store = archive(summarize=True)
    baseline, result = store.context(), store.context("北岸灯塔碰面暗号")
    assert "钴蓝纸鹤" not in str(baseline)
    recalled = result["recalled"]
    assert "钴蓝纸鹤" in str(recalled) and "周五" in str(recalled)
    assert result["summary"] == baseline["summary"]
    assert recalled[0]["data"]["sequence"] < recalled[-1]["data"]["sequence"]
    assert result["turns"][-1] == baseline["turns"][-1]
    assert all(item["version"] == store.turns[item["data"]["sequence"]]["version"] for item in recalled)
    assert not any(item["data"]["id"] in {row["data"]["id"] for row in result["turns"]} for item in recalled)


def test_existing_expansions_recover_long_foreign_language_evidence_with_original_offsets():
    store = archive()
    store.turns[5] = turn(5, "Background details. " * 400 + "The cobalt lantern rendezvous is on Friday.")
    store.turns[6] = turn(6, "Confirmed by the user.", speaker="assistant", status="sent")
    assert store.context("秘密会面地点")["recalled"] == []
    result = store.context("秘密会面地点", ["cobalt lantern rendezvous"])
    target = next(item for item in result["recalled"] if item["data"]["id"] == store.turns[5]["data"]["id"])
    assert "Friday" in target["data"]["content"]
    assert target["content_truncated"] and not target["excerpt"]["complete"]
    original = store.turns[5]["data"]["content"]
    assert target["data"]["content"] == "\n[…未提供的原文…]\n".join(
        original[start:end] for start, end in target["excerpt"]["ranges"])


@pytest.mark.parametrize("budget", [1000, 2000, 4000, 12000])
@pytest.mark.parametrize("summarize", [False, True])
def test_history_shares_existing_budget_and_keeps_latest_message(budget, summarize):
    store = archive(summarize=summarize, conversation_context_chars=budget)
    result = store.context("北岸灯塔碰面暗号")
    assert result["context_chars"] <= budget
    assert result["turns"][-1]["data"]["sequence"] == 59
    assert len(result["recalled"]) <= 6
    if result["recalled"]:
        assert "周五" in str(result["recalled"])  # Do not keep the assertion and silently drop its neighbor.
    measured = sum(size(item) for item in result["turns"])
    measured += size(result["summary"]) if result["summary"] else 0
    measured += size(result["recalled"]) if result["recalled"] else 0
    assert result["context_chars"] == measured


@pytest.mark.parametrize("query", ["", "unmatched-needle", "!!!"])
def test_no_match_does_not_shrink_recent_context(query):
    store = archive(conversation_context_chars=2000)
    assert store.context(query) == store.context()


def test_current_account_and_thread_bound_recall_even_if_other_archives_are_readable():
    store = archive()
    store.turns.extend([turn(70, "FOREIGN_THREAD_CANARY 北岸灯塔碰面暗号", thread="bob"),
                        turn(71, "FOREIGN_ACCOUNT_CANARY 北岸灯塔碰面暗号", account="other")])
    result = store.context("北岸灯塔碰面暗号")
    assert "钴蓝纸鹤" in str(result["recalled"])
    assert "FOREIGN_" not in str(result)


def test_disabled_and_legacy_config_without_recall_flag():
    store = archive()
    assert store.context("北岸灯塔")["recalled"]
    store.settings["conversation_recall_enabled"] = False
    assert store.context("北岸灯塔") == store.context()


def test_edit_and_delete_use_current_raw_turns_without_stale_recalled_text():
    store = archive(summarize=True)
    first = store.context("北岸灯塔")
    store.turns[5] = turn(5, "北岸灯塔改用琥珀风铃暗号。")
    edited = store.context("北岸灯塔")
    assert edited["summary"] is None and "琥珀风铃" in str(edited["recalled"])
    assert "钴蓝纸鹤" not in str(edited)
    del store.turns[5]
    assert "琥珀风铃" not in str(store.context("北岸灯塔"))
    assert "钴蓝纸鹤" in str(first)  # Previous snapshots were not mutated in place.


def test_attribution_and_unsent_status_are_preserved_in_recalled_neighbors():
    store = archive()
    store.turns[4] = turn(4, "北岸灯塔：林舟喜欢梨；林周喜欢苹果。", speaker="assistant", status="uncertain")
    result = store.context("北岸灯塔")
    rows = {item["data"]["sequence"]: item for item in result["recalled"]}
    assert rows[4]["data"]["status"] == "uncertain"
    assert rows[4]["data"]["speaker"] == "assistant" and rows[4]["data"]["sender_uid"] == "bot"
    assert rows[5]["data"]["speaker"] == "user" and rows[5]["data"]["sender_uid"] == "alice"


def test_overlapping_matches_do_not_duplicate_turns_or_change_sources():
    store = archive()
    store.turns[6] = turn(6, "北岸灯塔碰面暗号更正：琥珀风铃。")
    original = deepcopy(store.turns)
    result = store.context("北岸灯塔")
    ids = [item["data"]["id"] for item in result["recalled"]]
    assert len(ids) == len(set(ids)) and store.turns == original


def test_shared_neighbor_does_not_hide_a_second_matching_correction():
    store = archive()
    store.turns[7] = turn(7, "北岸灯塔的碰面暗号最终更正为琥珀风铃。")
    result = store.context("北岸灯塔碰面暗号")
    recalled = result["recalled"]
    assert {5, 7} <= {item["data"]["sequence"] for item in recalled}
    assert "钴蓝纸鹤" in str(recalled) and "琥珀风铃" in str(recalled)
    assert len({item["data"]["id"] for item in recalled}) == len(recalled)


@pytest.mark.parametrize("long", [False, True])
def test_two_distant_matches_share_recall_space_and_keep_both_versions(long):
    store = archive(summarize=True)
    store.turns[5] = turn(5, ("背景材料。" * 1000 if long else "") + "海角咖啡馆订位为周六下午三点。")
    store.turns[30] = turn(30, "海角咖啡馆订位已取消，周六不要去。")
    # Exercise realistic identifier overhead as well as a long first match.
    for item in store.turns:
        item["data"]["id"] = conversations.identifier("turn", item["data"]["id"])
    store.summaries = []
    result = store.context("海角咖啡馆订位的前后变化")
    assert "周六下午三点" in str(result["recalled"]) and "订位已取消" in str(result["recalled"])
    assert len({item["recall_anchor"] for item in result["recalled"]}) == 2
    assert result["context_chars"] <= 12000


def test_recall_can_read_a_summary_covered_prefix_of_a_partial_turn():
    store = archive()
    source = store.turns[5]
    store.summaries = [{"data": {"id": "partial", "account_uid": "bot",
        "thread_id": conversations.thread_id("bot", "local", "alice"), "content": "前文闲聊。",
        "previous": None,
        "turn_refs": [{"id": item["data"]["id"], "version": item["version"]} for item in store.turns[:6]],
        "turn_spans": [{"id": item["data"]["id"], "version": item["version"], "start": 0,
            "end": 15 if item is source else len(item["data"]["content"]), "total": len(item["data"]["content"])}
            for item in store.turns[:6]]}, "version": "partial-v1"}]
    result = store.context("北岸灯塔")
    assert result["summary"]["partial_turn"]["end"] == 15
    target = next(item for item in result["recalled"] if item["data"]["id"] == source["data"]["id"])
    assert target["data"]["content"] == source["data"]["content"]
    assert target["excerpt"]["ranges"] == [[0, len(source["data"]["content"])]]
