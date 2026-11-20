"""Opt-in, sequential real-model benchmark in disposable synthetic conversations."""
import json
import os
import random
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from companion_codex.app_server import RpcProcess, TurnEvidence
from companion_codex.model import CodexModel
from companion_core import events
from companion_core.contracts import Event
from companion_core.errors import AssistantError
from companion_core.observability import current_stage
from companion_core.reply_timing import control
from tests_postgres.test_daily_service import started

MARKER = "松果收银台"
HISTORY = (
    "每次来摊位玩，我都爱连问你几加三等于几。咱俩管这出叫松果收银台，算着算着摊主就要下班了。",
    "今天我把桌子搬到窗边了。", "楼下的树长高了。", "晚饭准备煮面。",
    "午后的风好大。", "六加三等于几？", "七加三等于几？",
)


def cases():
    models = os.getenv("QIBAN_COMPARE_MODELS", "").split(",")
    if not all(re.fullmatch(r"gpt-[\w.-]+", model) for model in models):
        return []
    random.Random(97).shuffle(models)
    return [(n, m) for n, order in enumerate((models, models[::-1]), 1)
            for m in order if m != "gpt-5" or n == 1]


@pytest.mark.skipif(os.getenv("QIBAN_COMPARE_LIVE") != "1", reason="requires real model quota")
@pytest.mark.parametrize(("sample", "model"), cases())
async def test_compare_reply(pg_store, monkeypatch, sample, model):
    assert not os.getenv("QIBAN_DESKTOP_TEST_ROOT")
    service, token = started(pg_store, daily_local_replies=10)
    current = pg_store.read("companion")
    pg_store.write("companion", {**current["data"], "conversation_context_turns": 4},
                   current["version"], "fixture")
    service.core.decisions.model = CodexModel(model=model, reasoning_effort="low")
    receipt = service.local.post(token, "八加三等于几？", uuid4().hex)
    event = Event.from_dict(events.get(pg_store, receipt["id"])["event"])
    for i, text in enumerate(HISTORY):
        when = datetime.now(timezone.utc) - timedelta(days=21-i if i < 5 else 0, minutes=10-i)
        prior = replace(event, native_id=f"history-{i}", text=text, occurred_at=when.isoformat())
        events.ingest(pg_store, prior)
        service.core.record_user_turn(prior, service.core.governed_context(prior))
    row = {"model": model, "effort": "low", "sample": sample, "thread_models": [], "rerouted": False}
    baseline = os.getenv("QIBAN_COMPARE_BASELINE") == "1"
    row["receive_checks"] = "previous_polling" if baseline else "boundaries"
    original_call, original_update = RpcProcess.call, TurnEvidence.update
    buffer = ""

    async def call(rpc, method, params, *args, **kwargs):
        if baseline and method == "turn/start":
            rpc.poll_permissions = True
        if method == "turn/start" and current_stage() == "reply.generate":
            material = json.loads(params["input"][0]["text"])
            row["old_joke_in_context"] = MARKER in json.dumps(material, ensure_ascii=False)
        response = await original_call(rpc, method, params, *args, **kwargs)
        if method == "thread/start":
            row["thread_models"].append(response.get("model"))
        return response

    def observe(evidence, message):
        nonlocal buffer
        if message.get("method") == "model/rerouted":
            row["rerouted"] = True
        if (current_stage() == "reply.generate" and "first_text_ms" not in row
                and message.get("method") == "item/agentMessage/delta"):
            buffer += message.get("params", {}).get("delta", "")
            if re.search(r'"text"\s*:\s*"[^"\s]', buffer):
                row["first_text_ms"] = round(1000*(time.perf_counter()-start), 1)
        original_update(evidence, message)

    monkeypatch.setattr(RpcProcess, "call", call)
    monkeypatch.setattr(TurnEvidence, "update", observe)
    control(pg_store, "arm", "local")
    start = time.perf_counter()
    try:
        result = await service.core.handle(event)
        row["status"] = result.get("action", {}).get("status", "unconfirmed")
    except AssistantError as error:
        row["status"] = error.code
    row["complete_ms"] = round(1000*(time.perf_counter()-start), 1)
    turns = pg_store.records("conversation_turns")[0]
    sent = [r["data"]["content"] for r in turns
            if r["data"]["speaker"] == "assistant" and r["data"]["status"] == "sent"]
    if len(sent) == 1:
        row["synthetic_reply"] = sent[0]
    spans = control(pg_store, "report", "local").get("spans", [])
    row["calls"] = [{"purpose": s["purpose"], "ms": s["duration_ms"],
                     "checks": s["guard_checks"], "check_ms": s["guard_ms"]}
                    for s in spans if s["stage"] == "model.request_and_wait"]
    with Path(os.environ["QIBAN_COMPARE_OUTPUT"]).open("a", encoding="utf8") as output:
        output.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(model, sample, row["status"], row["complete_ms"], flush=True)
    if row["status"] == "model_unavailable":
        return
    assert row["status"] == "succeeded" and row.get("synthetic_reply")
    assert row["thread_models"] == [model] and not row["rerouted"]
    assert row.get("old_joke_in_context") and row.get("first_text_ms")
    assert [call["purpose"] for call in row["calls"]] == ["reply.generate"]
    assert all((call["checks"] > 0) == baseline for call in row["calls"])
