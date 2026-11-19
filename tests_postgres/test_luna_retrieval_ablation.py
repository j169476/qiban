"""Paid opt-in experiment. Only synthetic turns in a fresh PostgreSQL schema."""
import asyncio
import json
import os
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from companion_codex import app_server
from companion_codex.app_server import RpcProcess, TurnEvidence
from companion_codex.model import CodexModel
from companion_core import conversation_recall, events, recall_routing
from companion_core.contracts import Event
from companion_core.decision import DecisionService
from companion_core.observability import current_stage, stage
from companion_core.reply_timing import control
from tests_postgres.test_daily_service import started

MODEL = "gpt-5.6-luna"
ARMS = ("current", "lexical", "vector", "hybrid", "expanded_hybrid")
CASES = {
    "joke": ("八加三等于几？", ("六加三等于几？", "七加三等于几？")),
    "correction": ("我周末去取陶杯，最终定在哪里、哪一天？", ()),
    "comfort": ("一天下来我像个被拧干的毛巾，陪我缓缓吧。", ()),
    "video": ("我上回说片子里对付忘词的那个办法，第一步具体怎么做？", ()),
    "unknown": ("我上次告诉过你我家的门禁密码是多少来着？", ()),
    "other_person": ("要给我做点心，我自己不能碰哪一种？", ()),
}
HOLDOUT = {
    "stage_fright": ("轮到我上台时大脑一片白，之前讲过一个不出声的小动作，你还记得怎么做吗？", ()),
    "different_code": ("想不起我家那串开门数字了，你记得吗？", ("拆包装时我看到过一串数字。",)),
    "new_joke": ("今天，九加三呢？", ("开摊了。", "七加三？", "八加三？")),
}

# Conflicting and unrelated neighbors make a matching topic insufficient.
HISTORY = (
    "每次来摊位玩，我都爱连问你几加三等于几。咱俩管这出叫松果收银台，算着算着摊主就要下班了。",
    "昨晚我把盐罐放错位置。", "茶杯包装上的防伪码是6098。",
    "我们周六要去南桥取陶杯。", "想买些黑巧克力。", "灯塔旁的公交站换名字了。",
    "今天排了好长的队。", "我取陶杯的安排改了，最终是周日去青瓦巷，之前的作废。",
    "插座接触不良，我把台灯搬走了。", "阿禾喜欢在累的时候列个清单。",
    "要是我累得没力气，你就说“把风留在门外”，然后听我讲，别给我开行动清单。",
    "地毯换成了绿色。", "朋友家的门禁是按指纹的。", "阿禾不能吃花生，不是我。",
    "今天把书架收拾好了。", "我自己对腰果过敏，别把我和阿禾弄反。",
    "我看过一个舞台表演视频，讲卡壳时先用指尖轻点左腕两下，再顺着节奏接话。我管它叫小节拍。",
    "另一个视频里老师教的是收拾厨房，第一步清空台面。",
    "路边那家店又换了招牌。", "午饭是蘑菇面。", "路边的小花开了。", "楼上装修结束了。",
)


@pytest.fixture(scope="session")
def encoder():
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    root = Path(os.environ["QIBAN_ABLATION_ENCODER"])
    tok = Tokenizer.from_file(str(root / "tokenizer.json"))
    tok.enable_truncation(max_length=512)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(root / "model_quantized.onnx"), options,
                                   providers=["CPUExecutionProvider"])

    def encode(texts):
        batch = tok.encode_batch(texts)
        inputs = {k: np.asarray([getattr(t, attr) for t in batch], dtype=np.int64)
                  for k, attr in (("input_ids", "ids"), ("attention_mask", "attention_mask"),
                                  ("token_type_ids", "type_ids"))}
        features = session.run(None, {x.name: inputs[x.name] for x in session.get_inputs()})[0][:, 0]
        return features / np.linalg.norm(features, axis=1, keepdims=True)
    return encode


def cases():
    arms = ("current", "prewarm") if os.getenv("QIBAN_ABLATION_WARM") == "1" else ARMS
    holdout_arm = os.getenv("QIBAN_ABLATION_HOLDOUT")
    if holdout_arm:
        selected = tuple(holdout_arm.split(","))
        assert all(arm in ARMS[1:] + ("prewarm",) for arm in selected)
        arms = ("current", *selected)
    for i, name in enumerate(HOLDOUT if holdout_arm else CASES):
        for arm in arms[i % len(arms):] + arms[:i % len(arms)]:
            yield name, arm


@pytest.mark.skipif(os.getenv("QIBAN_LUNA_ABLATION") != "1", reason="explicit paid live experiment")
@pytest.mark.parametrize(("name", "arm"), cases())
async def test_ablation(pg_store, monkeypatch, request, name, arm):
    monkeypatch.setenv("QIBAN_HYBRID_HISTORY_DIR", "off")  # Reproduce the original experimental arms.
    assert not os.getenv("QIBAN_DESKTOP_TEST_ROOT")
    service, token = started(pg_store, daily_local_replies=10)
    cfg = pg_store.read("companion")
    pg_store.write("companion", {**cfg["data"], "conversation_context_turns": 4}, cfg["version"], "fixture")
    service.core.decisions.model = CodexModel(model=MODEL, reasoning_effort="low")
    query, extra = (HOLDOUT if name in HOLDOUT else CASES)[name]
    receipt = service.local.post(token, query, uuid4().hex)
    event = Event.from_dict(events.get(pg_store, receipt["id"])["event"])
    corpus = HISTORY + extra
    for i, body in enumerate(corpus):
        then = datetime.now(timezone.utc) - timedelta(days=len(corpus)-i)
        previous = replace(event, native_id=f"seed-{i}", occurred_at=then.isoformat(), text=body)
        events.ingest(pg_store, previous)
        service.core.record_user_turn(previous, service.core.governed_context(previous))
    row = {"case": name, "arm": arm, "models": [], "rerouted": False}
    if arm in ("lexical", "vector", "hybrid"):
        resolve = recall_routing.resolve

        async def without_expansion(*args, **kwargs):
            kwargs["remaining"] = 1  # Existing local route; actual reply budget unchanged.
            return await resolve(*args, **kwargs)
        monkeypatch.setattr(recall_routing, "resolve", without_expansion)
    if arm in ("vector", "hybrid", "expanded_hybrid"):
        encode = request.getfixturevalue("encoder")
        index_start = time.perf_counter()
        index = dict(zip(corpus, encode(list(corpus)), strict=True))
        row["build_ms"] = round(1000*(time.perf_counter()-index_start), 1)
        lexical = conversation_recall.scores

        def rank(records, query, queries=(), *, passages=None):
            tick = time.perf_counter()
            q = encode(["为这个句子生成表示以用于检索相关文章：" + query])[0]
            dense = {(r["kind"], r["data"]["id"]): float(index[r["data"]["content"]] @ q)
                     for r in records}
            if arm == "vector":
                scores = dense
            else:
                sparse = lexical(records, query, queries, passages=passages)
                scores = {}
                for ranking in (dense, sparse):
                    for position, key in enumerate(sorted((k for k in ranking if ranking[k] > 0),
                                                          key=ranking.get, reverse=True), 1):
                        scores[key] = scores.get(key, 0.) + 1 / (60 + position)
            row["ranking_ms"] = round(1000*(time.perf_counter()-tick), 1)
            return scores
        monkeypatch.setattr(conversation_recall, "scores", rank)

    call, update = RpcProcess.call, TurnEvidence.update
    buffer = ""
    warm = None
    handoff = asyncio.Event()

    if arm == "prewarm":
        generate = app_server.generate
        prompt, output = DecisionService.specification(
            {"private_growth": {}, "personal_facts": {}}, service.local.capabilities())
        payload, actual = {}, {}

        class Usage:
            def tokens(self, values):
                if actual:
                    actual["observation"].tokens(values)

        async def overlap(instructions, context, schema, images=(), **opts):
            nonlocal warm
            if current_stage() == "recall.expand":
                assert warm is None and not opts["native"]
                with stage("reply.generate"):
                    warm = asyncio.create_task(generate(prompt, payload, output, **{
                        **opts, "guard": lambda: actual.get("guard", service.core.guard)(),
                        "phase": lambda value: actual["phase"](value) if actual else None,
                        "observation": Usage()}))
                return await generate(instructions, context, schema, images, **opts)
            assert current_stage() == "reply.generate" and not images and not opts["native"]
            assert instructions == prompt and schema == output and warm is not None
            assert opts["model"] == MODEL and opts["reasoning_effort"] == "low"
            actual.update(opts)
            payload.update(context)
            handoff.set()
            return await warm
        monkeypatch.setattr(app_server, "generate", overlap)

    async def observe_call(rpc, method, params, *args, **kwargs):
        if method == "turn/start" and current_stage() == "reply.generate":
            material = json.loads(params["input"][0]["text"])
            row["recalled"] = [t["data"]["content"] for t in material["conversation"]["recalled"]]
        value = await call(rpc, method, params, *args, **kwargs)
        if method == "mcpServerStatus/list" and warm is asyncio.current_task():
            row["warm_ready_ms"] = round((time.perf_counter()-start)*1000, 1)
            await handoff.wait()
        if method == "thread/start":
            row["models"].append(value.get("model"))
        return value

    def observe(evidence, message):
        nonlocal buffer
        if message.get("method") == "model/rerouted":
            row["rerouted"] = True
        if (current_stage() == "reply.generate" and "first_ms" not in row
                and message.get("method") == "item/agentMessage/delta"):
            buffer += message.get("params", {}).get("delta", "")
            if re.search(r'"text"\s*:\s*"[^"\s]', buffer):
                row["first_ms"] = round((time.perf_counter()-start)*1000, 1)
        update(evidence, message)
    monkeypatch.setattr(RpcProcess, "call", observe_call)
    monkeypatch.setattr(TurnEvidence, "update", observe)
    control(pg_store, "arm", "local")
    start = time.perf_counter()
    try:
        result = await service.core.handle(event)
    finally:
        if warm is not None:
            warm.cancel()
            await asyncio.gather(warm, return_exceptions=True)
    row["complete_ms"] = round((time.perf_counter()-start)*1000, 1)
    row["status"] = result["action"]["status"]
    row["reply"], = [r["data"]["content"] for r in pg_store.records("conversation_turns")[0]
                     if r["data"]["speaker"] == "assistant" and r["data"]["status"] == "sent"]
    spans = control(pg_store, "report", "local")["spans"]
    row["calls"] = [s["purpose"] for s in spans if s["stage"] == "model.request_and_wait"]
    with Path(os.environ["QIBAN_ABLATION_OUTPUT"]).open("a", encoding="utf8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(name, arm, row["complete_ms"], flush=True)
    expected = ["reply.generate"] if arm in ("lexical", "vector", "hybrid") else ["recall.expand", "reply.generate"]
    assert row["status"] == "succeeded" and row["calls"] == expected
    assert row["models"] == [MODEL]*len(expected) and not row["rerouted"] and row.get("first_ms")


@pytest.mark.skipif(os.getenv("QIBAN_ABLATION_SCALE") != "1", reason="local encoder footprint experiment")
def test_encoder_footprint(request):
    import numpy as np

    start = time.perf_counter()
    encode = request.getfixturevalue("encoder")
    opened = time.perf_counter()
    texts = [f"记录 {i}。{HISTORY[i % len(HISTORY)]}" for i in range(1024)]
    vectors = np.concatenate([encode(texts[i:i+32]) for i in range(0, len(texts), 32)])
    indexed = time.perf_counter()
    samples = []
    for query, _ in (*CASES.values(), *HOLDOUT.values()):
        tick = time.perf_counter()
        q = encode(["为这个句子生成表示以用于检索相关文章：" + query])[0]
        top = np.argsort(vectors @ q)[-2:]
        assert len(top) == 2
        samples.append((time.perf_counter()-tick)*1000)
    result = {"documents": len(texts), "dimensions": vectors.shape[1], "bytes": vectors.nbytes,
              "open_ms": round((opened-start)*1000, 1), "index_ms": round((indexed-opened)*1000, 1),
              "query_ms": [round(t, 2) for t in samples]}
    Path(os.environ["QIBAN_ABLATION_OUTPUT"]).write_text(json.dumps(result), encoding="utf8")
