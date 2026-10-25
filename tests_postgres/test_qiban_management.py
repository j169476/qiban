from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.observability import measure
from tests_postgres.test_web import client, login
from tests_postgres.test_web import service as service


def test_monitor_requires_admin_and_does_not_double_count_model_tokens(service):
    role = client(service, "role")
    headers = login(role, service, "role")
    assert role.post("/operate", json={"operation": "runtime_monitor", "arguments": {}}, headers=headers).status_code == 404
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    with measure(service.store, "synthetic", "stage") as stage:
        stage.tokens({"input_tokens": 1000, "output_tokens": 100})
        with measure(service.store, "synthetic", "model") as model:
            model.tokens({"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 20,
                          "reasoning_output_tokens": 10, "usage_events": 1})
    reply = admin.post("/operate", json={"operation": "runtime_monitor", "arguments": {}}, headers=headers)
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data["tokens"] == {"input_tokens": 100, "cached_input_tokens": 30,
                              "output_tokens": 20, "reasoning_output_tokens": 10}
    assert data["model_calls"] == 1
    assert data["coding_plan"]["status"] == "unavailable"
    assert service.status()["status"] == "paused"
    assert admin.post("/operate", json={"operation": "runtime_monitor", "arguments": {"days": 0}}, headers=headers).status_code == 403


def test_web_start_uses_bound_admin_and_stale_stop_cannot_stop_new_generation(service):
    service.control("synthetic-bridge-secret", "admin", "management_open", {})
    admin = client(service, "admin")
    headers = login(admin, service, "admin")

    def control(action, generation=None):
        return admin.post("/operate", json={"operation": "platform_control", "arguments": {
            "platform": "local", "action": action, "expected_generation": generation, "options": {}}}, headers=headers)

    reply = control("start")
    assert reply.status_code == 200, reply.text
    run = service.store.state("core-run")
    assert run["owner"] == "admin"
    assert control("stop").status_code == 409
    assert service.status()["status"] == "active"
    assert control("stop", run["generation"]).status_code == 200
    assert service.status()["status"] == "paused"
    service.host.archived = True
    assert control("start", run["generation"]).status_code == 403


def test_desktop_loss_latches_stop_and_only_explicit_admin_resume_unlocks(service):
    desktop = SimpleNamespace(status=lambda: {"available": True, "status": "online"}, adopt=lambda: None)
    service.host.desktop = desktop
    service.control("synthetic-bridge-secret", "admin", "management_open", {})
    service.control("synthetic-bridge-secret", "control", "assistant_start", {})
    desktop.status = lambda: {"available": False, "status": "stopped"}
    service.check_desktop()
    assert service.status()["status"] == "paused"
    assert service.status()["host"]["restart_required"]
    desktop.status = lambda: {"available": True, "status": "online"}
    with pytest.raises(AssistantError, match="重启"):
        service.control("synthetic-bridge-secret", "control", "assistant_start", {})
    with pytest.raises(AssistantError):
        service.control("synthetic-bridge-secret", "control", "service_resume", {})
    assert service.control("synthetic-bridge-secret", "admin", "service_resume", {})["role_started"] is False
    assert service.status()["status"] == "paused"
    assert not service.status()["host"]["restart_required"]


def test_task_cancel_is_versioned_and_does_not_requeue(service):
    service.store.set_state("explicit-task:synthetic", {"status": "running", "phase": "research", "revision": "r1"})
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    body = {"operation": "task_cancel", "arguments": {"id": "synthetic", "expected_revision": "old"}}
    assert admin.post("/operate", json=body, headers=headers).status_code == 409
    body["arguments"]["expected_revision"] = "r1"
    assert admin.post("/operate", json=body, headers=headers).status_code == 200
    assert service.store.state("explicit-task:synthetic")["status"] == "cancelled"


def test_open_codex_requires_admin_and_rejects_user_command_arguments(service):
    calls = []
    service.host.open_desktop = lambda: calls.append(True) or {"launch_requested": True, "role_started": False}
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    body = {"operation": "open_codex", "arguments": {"command": "untrusted"}}
    assert admin.post("/operate", json=body, headers=headers).status_code == 403
    assert calls == []
    body["arguments"] = {}
    assert admin.post("/operate", json=body, headers=headers).json()["role_started"] is False
    assert calls == [True]


@pytest.mark.asyncio
async def test_service_recovery_after_abrupt_exit_requires_explicit_resume(service):
    service.host.desktop = SimpleNamespace(status=lambda: {"available": True, "status": "online"})
    service.store.set_state("service-heartbeat", {"at": 100, "busy": True})
    await service.open()
    try:
        assert service.status()["host"]["restart_required"]
        assert service.status()["status"] == "paused"
    finally:
        await service.close()


def test_monitor_includes_active_task_after_many_completed_tasks(service):
    for i in range(101):
        service.store.set_state(f"explicit-task:a{i:03}", {"status": "completed", "phase": "done"})
    service.store.set_state("explicit-task:z-current", {"status": "running", "phase": "research"})
    from companion_service.monitoring import snapshot
    tasks = snapshot(service)["tasks"]
    assert len(tasks) == 100 and tasks[0]["id"] == "z-current"


def test_group_usage_reports_reservations_and_expired_run_without_private_content(service, monkeypatch):
    from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, ReplyTarget
    from companion_core.reply_policy import Participation
    from companion_service.reply_management import ReplyManagement

    scope = ReplyScope("qq", "10001", "group", "10003")
    ReplyAuthorizations(service.store).update(scope, enabled=True, expected_version=None, actor="test")
    target = ReplyTarget(scope, "合成测试群", "测试账号")
    monkeypatch.setattr(ReplyManagement, "targets", lambda self: ({scope.id: target}, {"qq": {"available": True}}))
    monkeypatch.setattr(service.platform_runs, "status", lambda p: {"status": "active", "generation": "run"})
    participation = Participation(service.store)
    ticket = participation.admit(scope, "synthetic-event", "mention", "run")
    participation.reserve(ticket, "run", "synthetic-action")
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    body = {"operation": "reply_policy_list", "arguments": {}}
    group = admin.post("/operate", json=body, headers=headers).json()["groups"][0]
    assert group["active"] and group["used"] == 1 and group["decisions"] == 1
    assert set(group) == {"platform", "label", "active", "settings", "used", "decisions", "expires", "reply_limit"}
    monkeypatch.setattr(service.platform_runs, "status", lambda p: {"status": "paused", "generation": "run"})
    assert not admin.post("/operate", json=body, headers=headers).json()["groups"][0]["active"]
