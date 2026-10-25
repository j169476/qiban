from types import SimpleNamespace

import pytest

from companion_core import budget
from companion_core.errors import AssistantError
from tests_postgres.test_web import service as service


def desktop(service):
    service.host.desktop = SimpleNamespace(status=lambda: {"available": True, "status": "online"})


def test_recovery_covers_orphaned_generations_without_refunding_or_replaying(service):
    desktop(service)
    service.store.set_state("service-session", {"closed": False, "phase": "ready"})
    policy = service.store.read("policy")["data"]
    budget.reserve(service.store, policy, service.core.clock())
    with service.store.db() as db:
        for state in ("prepared", "dispatching", "uncertain", "succeeded"):
            db.execute("INSERT INTO core_actions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                state, state, "removed-generation", "fixture", "bot", "message.reply",
                service.store.ledger_encode("core_actions.body", {}), state, None, "2026-10-26T00:00:00Z"))
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                state, state, "synthetic", "old-generation", "bot", "reply",
                service.store.ledger_encode("actions.payload", {}), state, "2026-10-26", 1, None,
                "2026-10-26T00:00:00Z", None, None))
    service.recover_startup()
    for table in ("core_actions", "actions"):
        with service.store.db() as db:
            assert dict(db.execute("SELECT id,status FROM " + table)) == {
                "prepared": "cancelled", "dispatching": "uncertain", "uncertain": "uncertain", "succeeded": "succeeded"}
    assert budget.status(service.store, policy, service.core.clock())["used"] == 1
    assert service.store.state("service-session")["previous_shutdown"] == "unclean"
    assert service.store.state("service-session")["phase"] == "ready"
    assert service.host_status()["restart_required"]
    assert service.host_status()["message"] == "服务等待明确重启"
    with pytest.raises(AssistantError):
        service.require_runtime()


def test_interruption_during_recovery_is_detected_without_a_heartbeat(service, monkeypatch):
    desktop(service)
    original = service._pause
    monkeypatch.setattr(service, "_pause", lambda reason: (_ for _ in ()).throw(RuntimeError("synthetic interruption")))
    with pytest.raises(RuntimeError):
        service.recover_startup()
    assert service.store.state("service-heartbeat") is None
    assert service.store.state("service-session")["phase"] == "recovering"
    assert not service.store.state("service-session")["closed"]
    monkeypatch.setattr(service, "_pause", original)
    service.recover_startup()
    assert service.store.state("service-session")["previous_shutdown"] == "unclean"
    assert service.host_status()["restart_required"]


@pytest.mark.asyncio
async def test_clean_close_is_recorded_but_next_start_still_does_not_resume(service):
    desktop(service)
    await service.open()
    assert service.store.state("service-session")["previous_shutdown"] == "first_start"
    await service.close()
    assert service.store.state("service-session")["closed"]
    service.recover_startup()
    assert service.store.state("service-session")["previous_shutdown"] == "clean"
    assert service.host_status()["restart_required"]
    assert service.status()["status"] == "paused"


@pytest.mark.asyncio
async def test_failed_shutdown_is_not_marked_clean(service, monkeypatch):
    await service.open()
    monkeypatch.setattr(service, "_pause", lambda reason: (_ for _ in ()).throw(RuntimeError("synthetic storage failure")))
    await service.close()
    assert not service.store.state("service-session")["closed"]
