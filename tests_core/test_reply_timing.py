import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from companion_core import reply_timing as timing
from companion_core.errors import AssistantError
from companion_core.storage import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "timing")


def event(platform="qq"):
    return SimpleNamespace(platform=platform, text="PRIVATE-CANARY", id="PRIVATE-ID")


def test_disabled_path_never_constructs_capture_or_persists_details(store, monkeypatch):
    def forbidden():
        raise AssertionError("detailed clock allocated while disabled")
    monkeypatch.setattr(timing, "Capture", forbidden)
    with timing.capture_reply(store, event()):
        timing.phase("context.build")
        with timing.span("model.total"):
            pass
    assert timing.control(store, "report") == {"status": "disabled"}


def test_next_matching_reply_only_and_no_content_stored(store):
    request = timing.control(store, "arm")
    with timing.capture_reply(store, event("wechat")):
        pass
    assert timing.control(store, "report")["status"] == "armed"
    with timing.capture_reply(store, event()):
        timing.phase("context.build")
        with timing.span("memory.recall"):
            pass
        timing.phase("delivery.finalize")
    report = timing.control(store, "report")
    assert report["id"] == request["id"] and report["status"] == "completed"
    assert report["analysis"]["total_ms"] == report["spans"][0]["duration_ms"]
    assert {row["stage"] for row in report["analysis"]["phases"]} == {"context.build", "delivery.finalize"}
    assert [r["stage"] for r in report["spans"]] == ["reply.total", "context.build", "memory.recall", "delivery.finalize"]
    assert "PRIVATE" not in json.dumps(report)
    with timing.capture_reply(store, event()):
        timing.phase("must_not_appear")
    assert timing.control(store, "report") == report
    assert timing.CURRENT.get() is None and timing.PARENT.get() is None


def test_expiration_busy_and_cancel_do_not_leave_recording_enabled(store, monkeypatch):
    monkeypatch.setattr(timing.time, "time", lambda: 1000)
    timing.control(store, "arm")
    with pytest.raises(AssistantError, match="已有"):
        timing.control(store, "arm")
    monkeypatch.setattr(timing.time, "time", lambda: 1601)
    with timing.capture_reply(store, event()):
        assert timing.CURRENT.get() is None
    assert timing.control(store, "report")["status"] == "expired"
    timing.control(store, "arm")
    with timing.capture_reply(store, event()):
        timing.control(store, "cancel")
        replacement = timing.control(store, "arm", "wechat")
    assert timing.control(store, "report") == replacement


def test_failed_reply_consumes_request_without_error_text(store):
    timing.control(store, "arm")
    with pytest.raises(ValueError):
        with timing.capture_reply(store, event()):
            timing.phase("delivery.dispatch")
            raise ValueError("PRIVATE-CANARY")
    report = timing.control(store, "report")
    assert report["status"] == "failed_or_cancelled"
    assert all(r["status"] == "failed_or_cancelled" for r in report["spans"])
    assert "PRIVATE" not in json.dumps(report)
    assert timing.CURRENT.get() is None


def test_only_one_concurrent_reply_claims_request(store):
    timing.control(store, "arm")
    def run(_):
        with timing.capture_reply(store, event()):
            return timing.CURRENT.get() is not None
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(run, range(8))) == 1


def test_nested_and_overlapping_children_are_not_double_counted(monkeypatch):
    clock = [0]
    monkeypatch.setattr(timing.time, "perf_counter", lambda: clock[0])
    capture = timing.Capture()
    root = capture.begin("root", None)
    a = capture.begin("a", 0)
    clock[0] = .02
    b = capture.begin("b", 0)
    clock[0] = .08
    capture.end(a)
    clock[0] = .1
    capture.end(b)
    capture.end(root)
    rows = capture.report()
    assert rows[0]["duration_ms"] == 100 and rows[0]["self_ms"] == 0
    assert rows[1]["duration_ms"] == rows[2]["duration_ms"] == 80


async def test_thread_context_and_cancellation_cleanup(store):
    timing.control(store, "arm")
    @timing.timed("thread.stage")
    def thread_job():
        return 7
    with pytest.raises(asyncio.CancelledError):
        with timing.capture_reply(store, event()):
            assert await asyncio.to_thread(thread_job) == 7
            raise asyncio.CancelledError()
    assert "thread.stage" in [r["stage"] for r in timing.control(store, "report")["spans"]]
    assert timing.CURRENT.get() is None


def test_diagnostic_write_failure_does_not_change_success(store, monkeypatch):
    timing.control(store, "arm")
    with timing.capture_reply(store, event()):
        monkeypatch.setattr(store, "set_state", lambda *a, **kw: (_ for _ in ()).throw(OSError("PRIVATE")))
    assert timing.CURRENT.get() is None


def test_report_is_bounded(store):
    timing.control(store, "arm")
    with timing.capture_reply(store, event()):
        for _ in range(300):
            with timing.span("repeated"):
                pass
    report = timing.control(store, "report")
    assert report["limited"] and len(report["spans"]) == 256
