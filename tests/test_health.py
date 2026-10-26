import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from bili_assistant.errors import AssistantError
from bili_assistant.health import Heartbeat, deliver_faults, fail, faults, reconcile
from bili_assistant.host import CodexHost
from bili_assistant.scheduler import Scheduler
from bili_assistant.storage import utcnow


def enable_alerts(admin):
    doc = admin.store.read("companion")
    admin.config_patch("companion", {"failure_notifications": "admin_once"}, doc["version"])
    sent = []
    admin.host.notification_ready = lambda tid: admin.host.require_active(tid)
    admin.host.notify_admin = lambda tid, text, key: sent.append((tid, text, key)) or {"turn_id": "notice"}
    return sent


def managed(role, old=False):
    run = role.run()
    stamp = datetime.now(timezone.utc) - timedelta(minutes=10 if old else 0)
    run.update(background_requested=True, launch_requested_at=stamp.isoformat())
    role.store.set_state("run", run)
    return run


def test_heartbeat_changes_without_model_or_platform_work(store):
    with Heartbeat(store, "example") as beat:
        beat.pulse(phase="waiting", wait_reason="outside_hours")
        state = store.state("health:example")
        assert state["phase"] == "waiting" and state["wait_reason"] == "outside_hours"
        assert state["pid"] > 0 and state["at"]
    assert store.state("health:example")["phase"] == "stopped"


def test_stale_run_is_paused_and_observation_never_restarts_it(role, admin):
    run = managed(role, old=True)
    pending = role.store.state("model-cycles:123:2026-10-20", 7)
    assert reconcile(role)["status"] == "paused"
    assert reconcile(role)["reason"] == "supervisor_lost"
    assert len(faults(role.store)) == 1
    assert admin.status()["health"]["recovery"] == "explicit_start_only"
    assert role.store.state("run")["generation"] == run["generation"]
    assert role.store.state("model-cycles:123:2026-10-20", 7) == pending
    role.host.states["role"] = True
    role.host.states["role"] = False
    assert role.load()["run"]["status"] == "paused"
    # Explicit start creates a new generation but carries local pending-work scope.
    restarted = role.start()["run"]
    assert restarted["generation"] != run["generation"]
    assert restarted["pending_since"] == run["pending_since"]
    assert not restarted.get("background_requested")


def test_startup_grace_and_missing_child_heartbeat(role):
    run = managed(role)
    assert reconcile(role)["status"] == "active"
    run["launch_requested_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    role.store.set_state("run", run)
    role.store.set_state(f"supervisor:{run['generation']}", {"at": utcnow(), "phase": "monitoring"})
    assert reconcile(role)["reason"] == "heartbeat_lost"


def test_faults_once_admin_only_and_no_notice_for_ordinary_stops(role, admin):
    sent = enable_alerts(admin)
    generation = role.run()["generation"]
    fail(role, "repeated_failure", generation)
    fail(role, "daemon_exited", generation)
    assert deliver_faults(admin) == [generation]
    assert deliver_faults(admin) == []
    assert len(sent) == 1 and sent[0][0] == "admin"
    assert "角色已暂停" in sent[0][1]
    assert role.store.state("run")["status"] == "paused"
    second = role.start()["run"]["generation"]
    fail(role, "chat_archived", second)
    assert len(faults(role.store)) == 1
    assert deliver_faults(admin) == []


def test_stale_explicit_destination_cannot_override_current_binding(role, admin):
    sent = enable_alerts(admin)
    with admin.store.db() as db:
        db.execute("INSERT INTO sessions VALUES (?,?)", ("old-admin", "admin"))
    doc = admin.store.read("companion")
    admin.config_patch("companion", {"management_thread_id": "old-admin"}, doc["version"])
    generation = role.run()["generation"]
    fail(role, "login_required", generation)
    assert deliver_faults(admin) == [generation]
    assert [item[0] for item in sent] == ["admin"]


def test_fault_owner_is_current_admin_still_receives_once(role, admin):
    sent = enable_alerts(admin)
    generation = role.run()["generation"]
    fault = fail(role, "login_required", generation)
    admin.store.set_state("fault:" + generation, {**fault, "owner": "admin"})
    assert deliver_faults(admin) == [generation]
    assert deliver_faults(admin) == [] and len(sent) == 1


def test_rebinding_during_host_check_leaves_fault_for_new_admin(role, admin):
    sent = enable_alerts(admin)
    generation = role.run()["generation"]
    fail(role, "login_required", generation)

    def rebind(_):
        with admin.store.lock, admin.store.db() as db:
            db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT DO NOTHING", ("new-admin", "admin"))
            admin.store.set_state("management_thread", "new-admin", db)

    admin.host.notification_ready = rebind
    assert deliver_faults(admin) == [] and not sent
    assert faults(admin.store)[0]["notification"] == "queued"
    admin.host.notification_ready = lambda tid: admin.host.require_active(tid)
    assert deliver_faults(admin) == [generation]
    assert [item[0] for item in sent] == ["new-admin"]


def test_admin_role_revoked_during_host_check_cannot_receive(role, admin):
    sent = enable_alerts(admin)
    generation = role.run()["generation"]
    fail(role, "login_required", generation)

    def revoke(_):
        with admin.store.db() as db:
            db.execute("UPDATE sessions SET mode='role' WHERE thread='admin'")

    admin.host.notification_ready = revoke
    assert deliver_faults(admin) == [] and not sent
    assert faults(admin.store)[0]["notification"] == "queued"


def test_archived_or_unbound_destination_stays_queued_and_ambiguous_send_never_retries(role, admin):
    sent = enable_alerts(admin)
    generation = role.run()["generation"]
    fail(role, "login_required", generation)
    admin.host.states["admin"] = True
    assert deliver_faults(admin) == [] and not sent
    admin.host.states["admin"] = False
    admin.store.set_state("management_thread", "role")
    assert deliver_faults(admin) == []
    admin.store.set_state("management_thread", "admin")

    def lost_response(*args):
        sent.append(args)
        raise TimeoutError("fake-secret")

    admin.host.notify_admin = lost_response
    assert deliver_faults(admin) == []
    assert faults(admin.store)[0]["notification"] == "uncertain"
    assert deliver_faults(admin) == [] and len(sent) == 1
    assert "fake-secret" not in json.dumps(faults(admin.store))


def test_new_generation_and_manual_pause_do_not_create_an_old_fault(role):
    old = role.run()["generation"]
    role.stop(paused=True)
    new = role.start()["run"]["generation"]
    assert fail(role, "daemon_exited", old) is None
    assert role.run()["generation"] == new
    assert not faults(role.store)


def test_inflight_action_becomes_uncertain_on_interruption_and_cannot_replay(role):
    action = role.prepare("dynamic", {"text": "fixture"})
    with role.store.db() as db:
        db.execute(
            "UPDATE actions SET status='dispatching',day='2026-10-20' WHERE id=?", (action["id"],)
        )
    fail(role, "daemon_exited", role.run()["generation"])
    with role.store.db() as db:
        row = db.execute("SELECT status,result FROM actions WHERE id=?", (action["id"],)).fetchone()
    assert row["status"] == "uncertain"
    assert json.loads(row["result"])["error"] == "interrupted"
    assert role.status()["quota"]["dynamic"]["used"] == 1


def test_account_change_blocks_reads_as_well_as_writes(role):
    manifest = role.store.read("manifest")
    manifest["data"]["account_uid"] = "999"
    role.store.write("manifest", manifest["data"], manifest["version"], "test")
    with pytest.raises(AssistantError, match="账号绑定"):
        role.run(check_host=False)


@pytest.mark.asyncio
async def test_three_failures_pause_and_ordinary_retries_are_silent(role, monkeypatch):
    import bili_assistant.scheduler as module

    generation = role.run()["generation"]
    scheduler = Scheduler(role, generation)
    clock = [0]
    monkeypatch.setattr(module.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    failures = []

    async def broken():
        assert not faults(role.store)
        failures.append(1)
        raise AssistantError("read_timeout", "fixture timeout")

    async def advance(seconds):
        clock[0] += 900

    scheduler.cycle = broken
    monkeypatch.setattr(module.asyncio, "sleep", advance)
    await scheduler.run()
    assert len(failures) == 3
    assert role.store.state("run")["reason"] == "repeated_failure"
    assert len(faults(role.store)) == 1
    pulse = role.store.state(f"health:{generation}")
    assert pulse["phase"] == "stopped" and pulse["failures"] == 3


@pytest.mark.asyncio
async def test_supervisor_observes_real_child_exit_without_respawning(role, monkeypatch, tmp_path):
    import bili_assistant.supervisor as module

    role.host.project = tmp_path
    generation = managed(role)["generation"]
    child = tmp_path / "exit_child.py"
    child.write_text("raise SystemExit(23)\n")
    monkeypatch.setattr(module, "command", lambda *_: [sys.executable, str(child)])
    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.01)
    await module.supervise(role, generation)
    assert role.store.state("run")["reason"] == "daemon_exited"
    assert len(faults(role.store)) == 1
    assert role.store.state(f"supervisor:{generation}")["phase"] == "stopped"


def test_notification_uses_only_explicit_destination_and_preserves_host_settings(tmp_path, monkeypatch):
    calls = []
    host = CodexHost(tmp_path)
    host.require_active = lambda tid: calls.append(("active", tid))

    def request(method, params):
        calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"status": {"type": "idle"}}}
        if method == "thread/resume":
            return {}
        if method == "turn/start":
            return {"turn": {"id": "notice-turn"}}
        if method == "thread/turns/list":
            return {"data": [{"id": "notice-turn", "status": "completed"}]}
        pytest.fail(method)

    host.request = request
    result = host.notify_admin("admin", "fixture alert", "incident")
    assert result["turn_id"] == "notice-turn"
    methods = [name for name, _ in calls]
    assert "thread/unarchive" not in methods
    start = next(params for name, params in calls if name == "turn/start")
    assert start["threadId"] == "admin" and start["input"] == []
    assert start["toolOutput"]["namespace"] == "bili_assistant"
    assert not {"model", "sandboxPolicy", "approvalPolicy", "disabledPluginIds"} & start.keys()
    host.request = lambda *_: {"thread": {"status": {"type": "active"}}}
    with pytest.raises(AssistantError, match="管理聊天"):
        host.notification_ready("admin")


@pytest.mark.asyncio
async def test_busy_management_notification_retries_when_idle_without_role_restart(role, admin, monkeypatch):
    import bili_assistant.health as module

    sent = enable_alerts(admin)
    generation = role.run()["generation"]
    fail(role, "login_required", generation)
    admin.host.states["admin"] = True
    waits = []

    async def becomes_available(seconds):
        waits.append(seconds)
        assert not sent and role.store.state("run")["status"] == "paused"
        admin.host.states["admin"] = False

    import asyncio

    monkeypatch.setattr(asyncio, "sleep", becomes_available)
    await module.notification_loop(admin)
    assert waits == [60] and len(sent) == 1
    assert admin.store.state("notifier")["status"] == "stopped"
    assert role.store.state("run")["generation"] == generation


def test_foreground_status_consumes_stop_hook_without_starting_background(role):
    generation = role.run()["generation"]
    (role.store.runtime / f"stop-{generation}").write_text("host_session_end")
    assert role.status()["run"]["status"] == "paused"
    assert not faults(role.store)


def test_revoking_notification_during_host_check_prevents_dispatch(role, admin):
    sent = enable_alerts(admin)
    fail(role, "login_required", role.run()["generation"])

    def revoked(_):
        doc = admin.store.read("companion")
        admin.config_patch("companion", {"failure_notifications": "off"}, doc["version"])

    admin.host.notification_ready = revoked
    assert deliver_faults(admin) == []
    assert not sent and faults(admin.store)[0]["notification"] == "queued"


@pytest.mark.asyncio
async def test_resume_preserves_received_but_unprocessed_message_and_not_historical_messages(role):
    original = role.run()
    event = {
        "id": "pending-dm", "kind": "dm_reply", "uid": "456",
        "received": original["started_at"], "text": "尚未处理的邀请",
    }
    with role.store.db() as db:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                   (event["id"], "123", "456", "dm_reply", event["received"], json.dumps(event)))
    role.stop(paused=True)
    role.clock = lambda: datetime.fromisoformat(original["started_at"]) + timedelta(hours=1)
    role.start()
    action = role.prepare("dm_reply", {"event_id": event["id"], "text": "稍后接着讨论"})
    assert (await role.execute(action["id"]))["sent"]
    assert len(role.adapter.sent) == 1
