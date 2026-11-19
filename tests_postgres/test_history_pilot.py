"""Opt-in paired, real-model trials with synthetic history and an in-memory reply adapter."""
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path

import pytest

from companion_codex.app_server import RpcProcess, TurnEvidence
from companion_codex.model import CodexModel
from companion_core.contracts import ActorRef
from companion_core.observability import current_stage
from companion_core.runtime import Companion
from tests_postgres.test_postgres_conversation_recall import (
    Adapter,
    Host,
    Model,
    event,
    record,
    restrict,
    seed,
)

MODEL = "gpt-5.6-luna"
A, B = "10217", "20931"
POTS = {2: (A, "我养的那盆薄荷取名叫海盐。"), 11: (B, "我养的那盆薄荷取名叫雨灯。")}
BRIDGE = {2: (A, "我在桥边摆摊，自己的摊位编号是红杏。"),
          8: (B, "我也在桥边摆摊，我的摊位编号是白榆。"),
          17: (A, "桥边摆摊的编号有调整，我这个改成了青苔，原先的编号作废。")}
CASES = {
    "group_a": ("group", A, "上次说我给那盆薄荷取的什么名字来着？", POTS),
    "group_b": ("group", B, "上次说我给那盆薄荷取的什么名字来着？", POTS),
    "replacement": ("private", A, "我取陶杯最后约在哪天、什么地方？", {
        2: (A, "取陶杯约在周六去南桥。"), 17: (A, "店家发消息了，取杯子改在周日的青瓦巷。")}),
    "withdrawal": ("private", A, "我现在约了哪天去学木刻？", {
        2: (A, "跟老师约了周五去上木刻课。"), 17: (A, "木刻课先取消了，新的日子等老师通知。")}),
    "group_changed": ("group", A, "现在我在桥边摆摊用哪个编号？", BRIDGE),
    "group_unchanged": ("group", B, "现在我在桥边摆摊用哪个编号？", BRIDGE),
    "unseen_exchange": ("group", B, "我自己的换书地点最后定在哪儿来着？", {
        1: (B, "我周末跟人换书，碰头地点在石阶。"), 10: (A, "我也换书，我的碰头地点在长廊。"),
        18: (B, "我换书的地方挪到了榕树下，免得晒。")}),
    "unseen_named_other": ("group", A, "帮我想想，阿叶选的是哪种布料？", {
        3: (B, "我是阿叶，我选的布料是灯芯绒。"), 15: (A, "我是阿舟，我选的布料是粗麻。")}),
    "joke_control": ("private", A, "那九加三呢？", {
        2: (A, "每回我玩松果收银台，就来考你几加三，逗你玩。"), 24: (A, "七加三等于几？"),
        25: (A, "八加三呢？")}),
}
FILL = ("刚收好雨伞。", "楼下的猫睡着了。", "窗户修好了。", "午饭是面。", "小路旁开花了。",
        "今天风有点大。", "书架收拾完了。", "楼下新开了花店。", "去邮局寄了包裹。")


def setup_case(store, name):
    visibility, actor, query, history = CASES[name]
    setting = store.read("companion")
    store.write("companion", {**setting["data"], "conversation_context_turns": 4}, setting["version"], "pilot")

    def turn(index, text, speaker, thread="main", audience=visibility):
        return replace(event(index, text, actor=speaker, thread=thread), visibility=audience)

    with store.lock, store.db():
        for i in range(26):
            speaker, text = history.get(i, (A if visibility == "private" or i % 2 else B, FILL[i % len(FILL)]))
            record(store, turn(i, text, speaker))
        for i, thread, audience in ((80, "separate-group", "group"), (81, "separate-private", "private")):
            record(store, turn(i, query + " 另一个会话的暗记：越界紫杉。", actor, thread, audience))
    return turn(90, query, actor)


def pairs():
    for index, name in enumerate(CASES):
        for arm in (("current", "hybrid") if index % 2 == 0 else ("hybrid", "current")):
            yield name, arm


@pytest.mark.parametrize("failure", ["missing", "during_ranking"])
async def test_pilot_encoder_failure_restores_existing_expansion(pg_store, monkeypatch, tmp_path, failure):
    from companion_core.hybrid_history import HybridHistory

    monkeypatch.setenv("QIBAN_HYBRID_HISTORY_DIR", str(tmp_path / "missing-model"))
    seed(pg_store)
    model, adapter = Model(), Adapter()
    core = Companion(pg_store, model, Host(), [adapter])
    assert core.history_ranker is None

    class Broken:
        def encode(self, texts):
            raise RuntimeError("synthetic encoder failure")

    if failure == "during_ranking":
        core.history_ranker = HybridHistory(Broken())
    core.start()
    try:
        result = await core.handle(event(200, "之前那个碰面的暗号"))
        assert result["action"]["status"] == "succeeded" and len(adapter.sent) == 1
        assert len(model.expansions) == 1 and core.history_ranker is None
        assert "钴蓝纸鹤" in str(model.decisions[-1]["conversation"]["recalled"])
    finally:
        core.stop()


def test_governed_pilot_reranks_only_readable_turns_on_every_lookup(pg_store):
    from companion_core import conversations
    from companion_core.governance import GovernedContext
    from companion_core.retrieval import scores

    current = setup_case(pg_store, "group_b")
    tid = conversations.thread_id(current.account, current.platform, current.thread)
    seen = {}

    def rank(rows, query, queries, *, passages, current_sender):
        assert current_sender == current.actor.key
        for row in rows:
            seen[row["data"]["content"]] = row["data"]["id"]
        assert "越界紫杉" not in str(seen)
        return scores(rows, query, queries, passages=passages)

    def lookup():
        return GovernedContext(pg_store, current).conversation(current.account, tid, query=current.text,
                                                               ranker=rank, current_sender=current.actor.key)

    supplied = lookup()["recalled"]
    assert {row["from_current_sender"] for row in supplied} == {True, False}
    assert all(row["from_current_sender"] == (row["data"]["sender_uid"] == current.actor.key) for row in supplied)
    text = POTS[2][1]
    restrict(pg_store, "conversation_turns/" + seen[text])
    seen.clear()
    assert text not in str(lookup()) and text not in seen


def test_vector_cache_uses_current_document_versions_only():
    from companion_core.hybrid_history import HybridHistory

    class Vector(float):
        def __matmul__(self, other):
            return self * other

        def copy(self):
            return self

    class Encoder:
        def __init__(self):
            self.texts = []

        def encode(self, texts):
            self.texts.extend(texts)
            return [Vector(1) for _ in texts]

    encoder = Encoder()
    rank = HybridHistory(encoder)

    def lookup(content, version):
        return rank([{"kind": "conversation_turns", "version": version,
                      "data": {"id": "turn-1", "content": content}}], "碰头的地方？")

    original, corrected = "碰头在南桥", "碰头改在北岸"
    lookup(original, "first")
    lookup(original, "first")
    lookup(corrected, "second")
    assert encoder.texts.count(original) == encoder.texts.count(corrected) == 1
    assert rank([], "碰头的地方？") == {}


@pytest.mark.parametrize(("name", "source_index"), [
    ("group_a", 2), ("group_b", 11), ("replacement", 17), ("withdrawal", 17),
    ("group_changed", 17), ("group_unchanged", 8), ("unseen_exchange", 18), ("unseen_named_other", 3),
])
def test_local_evidence_includes_correct_speaker_and_revision(pg_store, name, source_index):
    from companion_core import conversations
    from companion_core.governance import GovernedContext
    from companion_core.hybrid_history import HybridHistory

    current = setup_case(pg_store, name)
    record(pg_store, current)
    rank = HybridHistory.configured()
    assert rank is not None
    sender = current.actor.key if current.visibility == "group" else None
    context = GovernedContext(pg_store, current).conversation(current.account,
        conversations.thread_id(current.account, current.platform, current.thread),
        query=current.text, ranker=rank, current_sender=sender)
    speaker, original = CASES[name][3][source_index]
    assert any(t["data"]["content"] == original and t["data"]["sender_uid"] == ActorRef("alpha", speaker).key
               for t in context["recalled"])


async def test_structured_recall_keeps_expansion_with_history_pilot(pg_store):
    from companion_core.governance import GovernedContext
    from companion_core.retrieval import scores

    seed(pg_store)
    settings = pg_store.read("companion")

    # Use approved memory records rather than changing the history-only router in the test.
    source = event(4, "北岸灯塔")
    owner = GovernedContext(pg_store, source)
    for i in range(settings["data"]["reflection_min_memories"]):
        key = f"pilot-memory-{i}"
        pg_store.write("memories", {"schema_version": 1, "id": key, "kind": "experience",
                       "content": "北岸灯塔", "tags": [], "locked": False, "confidence": "observed",
                       "source": {"type": "interaction", "recorded_at": source.occurred_at,
                                  "reference": source.id}}, None, "fixture", key)
        owner.classify_written("memories", key, sources=["event/" + source.id])
    model, adapter = Model(), Adapter()
    core = Companion(pg_store, model, Host(), [adapter])

    def rank(rows, query, queries, *, passages, current_sender):
        return scores(rows, query, queries, passages=passages)

    core.history_ranker = rank
    core.start()
    try:
        await core.handle(event(200, "之前那个碰面安排"))
        assert len(model.expansions) == 1 and len(adapter.sent) == 1
    finally:
        core.stop()


@pytest.mark.skipif(os.getenv("QIBAN_LIVE_HISTORY_PILOT") != "1", reason="paid, explicit opt-in")
@pytest.mark.parametrize(("name", "arm"), pairs())
async def test_paired_reply(pg_store, monkeypatch, name, arm):
    assert not os.getenv("QIBAN_DESKTOP_TEST_ROOT")
    output = Path(os.environ["QIBAN_HISTORY_PILOT_OUTPUT"])
    if arm == "current":
        monkeypatch.setenv("QIBAN_HYBRID_HISTORY_DIR", "off")
    current = setup_case(pg_store, name)
    adapter = Adapter()
    core = Companion(pg_store, CodexModel(model=MODEL, reasoning_effort="low"), Host(), [adapter])
    assert (core.history_ranker is not None) == (arm == "hybrid")
    row = {"case": name, "arm": arm, "question": current.text, "asker": current.actor.user_id,
           "calls": [], "models": [], "rerouted": False}
    actors = {ActorRef("alpha", actor).key: actor for actor in (A, B)}
    call, update = RpcProcess.call, TurnEvidence.update
    buffer = ""

    async def observe_call(rpc, method, params, *args, **kwargs):
        if method == "turn/start":
            row["calls"].append([current_stage(), params.get("effort")])
            if current_stage() == "reply.generate":
                material = json.loads(params["input"][0]["text"])
                assert "越界紫杉" not in str(material)
                row["recalled"] = [{"speaker": actors.get(t["data"]["sender_uid"], "unknown"),
                                    "from_current_sender": t.get("from_current_sender"),
                                    "time": t["data"]["occurred_at"], "text": t["data"]["content"]}
                                   for t in material["conversation"]["recalled"]]
        result = await call(rpc, method, params, *args, **kwargs)
        if method == "thread/start":
            row["models"].append(result.get("model"))
        return result

    def observe(evidence, message):
        nonlocal buffer
        if message.get("method") == "model/rerouted":
            row["rerouted"] = True
        if (current_stage() == "reply.generate" and "first_ms" not in row
                and message.get("method") == "item/agentMessage/delta"):
            buffer += message.get("params", {}).get("delta", "")
            if re.search(r'"text"\s*:\s*"[^"\s]', buffer):
                row["first_ms"] = round((time.perf_counter() - start) * 1000)
        update(evidence, message)

    monkeypatch.setattr(RpcProcess, "call", observe_call)
    monkeypatch.setattr(TurnEvidence, "update", observe)
    core.start()
    try:
        start = time.perf_counter()
        result = await core.handle(current)
        row["complete_ms"] = round((time.perf_counter() - start) * 1000)
        row["reply"] = [item["text"] for item in adapter.sent]
        row["action"] = (result.get("action") or {}).get("status")
        with pg_store.db() as db:
            row["usage"] = [dict(item) for item in db.execute(
                "SELECT stage, input_tokens, cached_input_tokens, output_tokens, usage_events "
                "FROM core_observations WHERE kind='model' ORDER BY started,id").fetchall()]
    finally:
        core.stop()
        with output.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    expected = ["reply.generate"] if arm == "hybrid" else ["recall.expand", "reply.generate"]
    assert row["calls"] == [[purpose, "low"] for purpose in expected]
    assert row["models"] == [MODEL] * len(expected) and not row["rerouted"]
    assert row.get("first_ms") and row["action"] == "succeeded" and len(row["reply"]) == 1
