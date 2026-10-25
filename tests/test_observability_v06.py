import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from bili_assistant.api import call
from bili_assistant.backups import create, restore
from bili_assistant.storage import Store
from companion_codex.model import CodexModel
from companion_codex.usage import from_trace
from companion_core.errors import AssistantError
from companion_core.observability import Observation, current_stage, measure, report


def test_usage_only_accepts_completion_numbers_and_preserves_unknown(tmp_path):
    trace = tmp_path / "events.jsonl"
    events = [[], {"type": "item.completed", "text": "secret", "usage": {"input_tokens": 999}},
              {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 8,
               "output_tokens": 5, "reasoning_output_tokens": 2}},
              {"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 12,
               "output_tokens": 6}}]
    trace.write_text("\n".join(json.dumps(event) for event in events) + "\ninvalid\n", encoding="utf-8")
    assert from_trace(trace) == {"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 11,
                                "reasoning_output_tokens": None, "usage_events": 2}
    trace.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": True,
                     "output_tokens": -1, "cached_input_tokens": "2"}}))
    assert from_trace(trace)["input_tokens"] is None and from_trace(trace)["output_tokens"] is None
    trace.write_text(json.dumps(events[2]) + '\n{"type":"turn.completed"}\n')
    assert from_trace(trace)["input_tokens"] is None and from_trace(trace)["usage_events"] == 2


def test_nested_stages_inherit_and_restore_with_safe_failure_code(store):
    assert current_stage() == "unclassified"
    with measure(store, "reflection"):
        assert current_stage() == "reflection"
        with pytest.raises(AssistantError):
            with measure(store, current_stage(), kind="model") as model:
                model.tokens({"input_tokens": 10, "output_tokens": 4, "usage_events": 1})
                raise AssistantError("model_timeout", "secret prompt and cookie")
    result = report(store)
    assert current_stage() == "unclassified"
    assert "secret" not in json.dumps(result)
    group = next(group for group in result["groups"] if group["kind"] == "model")
    assert group["stage"] == "reflection" and group["statuses"] == {"failed": 1}
    assert group["known_tokens"]["input_tokens"] == 10
    assert group["known_tokens"]["cached_input_tokens"] is None
    assert result["recent"][0]["error_code"] == "model_timeout"


def test_unknown_usage_does_not_masquerade_as_zero_and_limit_does_not_limit_totals(store):
    with measure(store, "reaction", "model"):
        pass
    with measure(store, "reaction", "model") as model:
        model.tokens({"input_tokens": 0, "output_tokens": 0, "usage_events": 1})
    result = report(store, limit=1)
    assert len(result["recent"]) == 1
    group = result["groups"][0]
    assert group["calls"] == 2 and group["known_tokens"]["input_tokens"] == 0
    assert group["token_known_calls"]["input_tokens"] == 1
    assert result["money_cost"] is None


def test_cancellation_and_crash_are_distinguishable(store):
    with pytest.raises(asyncio.CancelledError):
        with measure(store, "collect"):
            raise asyncio.CancelledError()
    interrupted = Observation(store, "reflection", "stage")
    interrupted.start()  # A killed host cannot execute finally.
    assert {row["status"] for row in report(store)["recent"]} == {"running", "cancelled"}


def test_retention_and_report_window(store):
    old = Observation(store, "old", "stage")
    old.start()
    timestamp = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    with store.db() as db:
        db.execute("UPDATE core_observations SET id=?,started=? WHERE id=?", (timestamp + "-old", timestamp, old.id))
    with measure(store, "new"):
        pass
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_observations").fetchone()[0] == 1
    assert len(report(store, days=30)["recent"]) == 1


def test_full_backup_preserves_metrics_and_marks_incomplete_work(store, tmp_path):
    with measure(store, "read", "model") as model:
        model.tokens({"input_tokens": 12, "output_tokens": 3, "usage_events": 1})
    interrupted = Observation(store, "reflect", "stage")
    interrupted.start()
    archive = tmp_path / "full.zip"
    create(store, archive)
    destination = tmp_path / "restored"
    restore(archive, destination)
    rows = report(Store(destination))["recent"]
    assert {row["status"] for row in rows} == {"interrupted", "success"}
    assert next(row for row in rows if row["kind"] == "model")["input_tokens"] == 12


@pytest.mark.asyncio
async def test_metrics_admin_only_and_read_does_not_start_role(role, admin):
    role.stop(paused=True)
    with pytest.raises(AssistantError):
        await call(role, "runtime_metrics", {})
    assert (await call(admin, "runtime_metrics", {}))["recent"] == []
    assert role.store.state("run")["status"] == "paused"


@pytest.mark.asyncio
async def test_local_worker_records_cli_usage_and_removes_raw_events(store, tmp_path):
    helper = tmp_path / "worker.py"
    helper.write_text('''import json,sys
from pathlib import Path
sys.stdin.read()
print(json.dumps({"type":"item.completed","text":"secret prompt"}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":17,"cached_input_tokens":10,"output_tokens":5}}))
Path(sys.argv[1]).write_text('{"ok":true}')
''', encoding="utf-8")
    model = CodexModel(command_factory=lambda work, output, schema: [sys.executable, str(helper), str(output)])
    with measure(store, "learning"):
        result = await model.generate("test", {}, {"type": "object"}, store=store,
                                      policy={"worker_timeout_seconds": 10}, guard=lambda: None)
    assert result == {"ok": True}
    rows = report(store)["recent"]
    assert next(row for row in rows if row["kind"] == "model")["input_tokens"] == 17
    assert "secret" not in json.dumps(rows)
    assert not list((store.runtime / "worker").iterdir())


@pytest.mark.asyncio
async def test_heartbeat_callback_failure_cannot_skip_usage_or_cleanup(store, tmp_path):
    helper = tmp_path / "worker.py"
    helper.write_text('''import sys
from pathlib import Path
sys.stdin.read()
print('{"type":"turn.completed","usage":{"input_tokens":42,"output_tokens":3}}')
Path(sys.argv[1]).write_text('{"ok":true}')
''', encoding="utf-8")
    model = CodexModel(command_factory=lambda work, output, schema: [sys.executable, str(helper), str(output)])

    def failed_phase(value):
        raise OSError("heartbeat storage unavailable")

    with pytest.raises(OSError):
        await model.generate("test", {}, {"type": "object"}, store=store,
                             policy={"worker_timeout_seconds": 10}, guard=lambda: None, phase=failed_phase)
    assert not list((store.runtime / "worker").iterdir())
    assert report(store)["recent"][0]["input_tokens"] == 42
    assert report(store)["recent"][0]["status"] == "failed"
