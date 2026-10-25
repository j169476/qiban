import os

import pytest

from bili_assistant.errors import AssistantError


def test_liveness_never_scans_content_collections(role, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("hot liveness guard enumerated content")

    monkeypatch.setattr(role.store, "records", forbidden)
    assert role.run()["status"] == "active"


def test_critical_edit_with_same_stamp_is_detected_immediately(role):
    path = role.store.path("policy")
    raw = path.read_bytes()
    stat = path.stat()
    path.write_bytes(raw.replace(b"autonomous", b"INVALID___"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(AssistantError, match="关键配置"):
        role.run()


def test_full_diagnostics_still_report_invalid_content(role):
    role.store.path("memories", "broken").write_text("not: [valid", encoding="utf-8")
    assert role.run()["status"] == "active"
    result = role.store.validate_all()
    assert not result["ok"] and result["issues"]
    with pytest.raises(AssistantError):
        role.store.read("memories", "broken")


def test_stop_marker_blocks_dispatch_before_any_full_scan(role, monkeypatch):
    run = role.run()
    (role.store.runtime / f"stop-{run['generation']}").write_text("stop", encoding="utf-8")
    monkeypatch.setattr(role.store, "records", lambda *args: pytest.fail("unexpected full scan"))
    with pytest.raises(AssistantError, match="停止标记"):
        role.prepare("like", {"bvid": "BV1xx411c7mD"})
