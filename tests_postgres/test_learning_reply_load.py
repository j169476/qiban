"""Opt-in, paid live benchmark. Never opens an existing desktop profile."""
import asyncio
import json
import math
import os
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from companion_codex.app_server import TurnEvidence
from companion_codex.isolation import model_settings
from companion_codex.model import CodexModel
from companion_core.reply_timing import control
from qiban.desktop_learning import digest
from qiban.desktop_management import DesktopManagement, provision_owner
from qiban.desktop_profile import desktop_profile
from qiban.desktop_runtime import DesktopRuntime
from tests_postgres.test_desktop_management import desktop_store
from tests_postgres.test_desktop_workspace import goal_fixture, read, ready

MODES = ("off", "pan", "rebuild")


def arm(root, stack, mode, model):
    root.mkdir()
    store = stack.enter_context(contextmanager(desktop_store.__wrapped__)(root, desktop_profile()))
    provision_owner(store, "benchmark", "synthetic-owner")
    manager = DesktopManagement(store, "benchmark", "synthetic-owner")
    scope = goal_fixture(manager)
    prefix = "desktop-tree:" + digest([scope[k] for k in ("account", "goal", "definition")])
    with store.db() as db:
        def records():
            for i in range(10000):
                key = f"note-{i:032x}"
                path = prefix + ":note:" + key
                note = {"id": key, "version": key, "title": f"案例 {i}", "body": "合成的学习笔记。",
                        "parent_id": f"note-{i-1:032x}" if 0 < i < 1000 else None, "archived": False,
                        "prerequisites": [f"note-{i-500:032x}"] if i > 1000 else []}
                yield path, store.ledger_encode("state:" + path, note)
        with db.connection.cursor() as cursor:
            cursor.executemany("INSERT INTO state(key,value) VALUES(%s,%s)", records())
    config = store.read("companion")
    store.write("companion", {**config["data"], "conversation_context_turns": 4}, config["version"], "benchmark")
    policy = store.read("policy")
    store.write("policy", {**policy["data"], "daily_local_replies": 200, "daily_model_cycles": 300},
                policy["version"], "benchmark")
    runtime = DesktopRuntime(manager, mode, room_model=model, adapters=[manager.room])
    stack.callback(runtime.close)
    runtime.start("local")
    version = ready(manager, scope)
    if mode == "off":
        read(manager, "learning.index_control", {"paused": True})
    return SimpleNamespace(manager=manager, core=runtime.cores["local"], scope=scope, version=version)


@pytest.mark.skipif(os.getenv("QIBAN_LIVE_LOAD") != "1", reason="explicit paid real-model benchmark")
@pytest.mark.asyncio
async def test_real_reply_under_learning_load(tmp_path, monkeypatch):
    assert not os.getenv("QIBAN_DESKTOP_TEST_ROOT")
    total = int(os.environ.get("QIBAN_LOAD_SAMPLES", "20"))
    assert 1 <= total <= 50
    path = Path(os.environ["QIBAN_LOAD_OUTPUT"])
    modes = (os.environ["QIBAN_LOAD_MODE"],) if os.getenv("QIBAN_LOAD_MODE") else MODES
    assert all(mode in MODES for mode in modes)
    detailed = os.getenv("QIBAN_LOAD_TIMING") == "1"
    settings = model_settings()
    model = CodexModel(model=settings.get("model"), reasoning_effort="low")
    original = TurnEvidence.update
    current = {}

    def observe(evidence, row):
        original(evidence, row)
        if not current or current.get("first_text") or row.get("method") != "item/agentMessage/delta":
            return
        chunk = row.get("params", {}).get("delta")
        if isinstance(chunk, str):
            current["buffer"] += chunk
            if re.search(r'"text"\s*:\s*"[^"\s]', current["buffer"]):
                current["first_text"] = time.perf_counter()
    monkeypatch.setattr(TurnEvidence, "update", observe)
    output = {"samples_per_mode": total, "model": model.model, "effort": model.reasoning_effort,
              "nodes": 10001, "concurrency": 1, "rows": []}
    with ExitStack() as stack:
        cases = {k: arm(tmp_path / k, stack, k, model) for k in modes}
        for i in range(total):
            for mode in modes[i % len(modes):] + modes[:i % len(modes)]:
                case = cases[mode]
                manager = case.manager
                bridge = threading.Lock()
                stop = threading.Event()
                entered = threading.Event()
                loads, errors = [], []
                def load():
                    while not stop.is_set():
                        try:
                            if mode == "rebuild":
                                # Drop only the synthetic process layout cache.
                                with manager.learning_index.lock:
                                    manager.learning_index.cache.clear()
                            with bridge:
                                stamp = time.perf_counter()
                                if mode == "rebuild":
                                    read(manager, "learning.snapshot", case.scope)
                                else:
                                    read(manager, "learning.tile", {**case.scope, "snapshot": case.version,
                                         "x": len(loads) % 9, "y": 0, "level": 1, "offset": 0})
                                loads.append(time.perf_counter() - stamp)
                        except Exception as exc:
                            errors.append(type(exc).__name__)
                            break
                        finally:
                            entered.set()
                        stop.wait(.1)
                worker = None
                if mode != "off":
                    worker = threading.Thread(target=load, daemon=True)
                    worker.start()
                    assert await asyncio.to_thread(entered.wait, 10)
                current.clear()
                current.update(buffer="", first_text=None)
                if detailed:
                    control(manager.store, "arm", "local")
                start = time.perf_counter()
                try:
                    with bridge:
                        acquired = time.perf_counter()
                        read(manager, "room.post", {"text": f"请用一句短句回答：{i+2}加三等于几？",
                             "session": manager.session_id, "nonce": uuid4().hex})
                    event, = await manager.room.runtime_poll()
                    handler_start = time.perf_counter()
                    result = await case.core.handle(event)
                    assert result["action"]["status"] == "succeeded"
                    end = time.perf_counter()
                    assert current["first_text"], "模型传输未提供可定位的正文增量"
                    row = {"mode": mode, "round": i+1, "bridge_ms": round((acquired-start)*1000, 1),
                           "first_text_ms": round((current["first_text"]-start)*1000, 1),
                           "complete_ms": round((end-start)*1000, 1), "tree_calls": len(loads)}
                    output["rows"].append(row)
                    if detailed:
                        report = control(manager.store, "report", "local")
                        assert report["status"] == "completed" and not report["limited"]
                        row.update(before_handler_ms=round((handler_start-start)*1000, 3),
                                   stages_ms=report["analysis"]["wall_by_stage_ms"],
                                   model_turns=[r for r in report["spans"] if r["stage"] == "model.request_and_wait"])
                    path.write_text(json.dumps(output, indent=2), encoding="utf8")
                    print(json.dumps(row), flush=True)
                finally:
                    stop.set()
                    if worker:
                        await asyncio.to_thread(worker.join)
                    current.clear()
                assert not errors
                while manager.learning_index.status()["pending"]:
                    await asyncio.sleep(.05)
    for group in modes:
        values = [r for r in output["rows"] if r["mode"] == group]
        print(group, {key: [sorted(r[key] for r in values)[math.ceil(n*total)-1] for n in (.5,.95)]
                      for key in ("bridge_ms","first_text_ms","complete_ms")}, flush=True)
