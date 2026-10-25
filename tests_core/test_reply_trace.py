import json
from types import SimpleNamespace

import pytest

from companion_core.observability import measure
from companion_core.reply_trace import CURRENT, listing, point, reply
from companion_core.storage import Store


def test_reply_correlates_stage_usage_and_outcome_without_message_text(tmp_path):
    store = Store(tmp_path / "trace")
    event = SimpleNamespace(id="event-safe", platform="wechat", text="private-canary")
    with reply(store, event, {"generation": "run-safe"}) as trace:
        with measure(store, "model", "model") as observation:
            observation.tokens({"input_tokens": 10, "output_tokens": 4})
        point("outcome", status="uncertain")
    saved = store.state("reply-trace:" + trace["id"])
    assert saved["steps"][0]["observation_id"] == observation.id
    assert saved["steps"][0]["output_tokens"] == 4
    assert saved["steps"][-1]["status"] == "uncertain"
    assert "private-canary" not in json.dumps(saved)
    assert listing(store)[0]["id"] == trace["id"] and CURRENT.get() is None


def test_trace_error_does_not_expose_exception_text_or_contaminate_next_reply(tmp_path):
    store = Store(tmp_path / "trace")
    event = SimpleNamespace(id="event-safe", platform="qq")
    with pytest.raises(ValueError):
        with reply(store, event, {"generation": "run-safe"}):
            raise ValueError("private-error-canary")
    assert listing(store)[0]["status"] == "failed"
    assert "private-error-canary" not in str(store.states("reply-trace:%"))
    assert CURRENT.get() is None


def test_trace_failure_never_converts_successful_action_into_retry(tmp_path, monkeypatch):
    store = Store(tmp_path / "trace")
    def unavailable(*args, **kwargs):
        raise OSError("synthetic")
    monkeypatch.setattr(store, "set_state", unavailable)
    with reply(store, SimpleNamespace(id="e", platform="qq"), {"generation": "g"}):
        point("outcome", status="succeeded")
    assert store._reply_trace_failures == 2
