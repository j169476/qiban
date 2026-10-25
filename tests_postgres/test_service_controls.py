import pytest

from companion_core import events
from companion_core.contracts import Event
from companion_core.errors import AssistantError
from companion_service.admin import Management
from companion_service.service import Service
from tests_postgres.test_runtime import Model
from tests_postgres.test_web import PASSWORD, Host


def prepared(store):
    service = Service(store, Model(), Host(), bridge_secret="synthetic-bridge-secret")
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    return service


def test_codex_and_web_use_identical_admin_cas(pg_store):
    service = prepared(pg_store)
    service.control("synthetic-bridge-secret", "admin", "management_open", {})
    token = service.sessions.login(PASSWORD, "admin")
    request = service.sessions.request(token, "admin")
    prior = service.control("synthetic-bridge-secret", "admin", "config_get", {"kind": "policy"})
    first = Management(service).operation(request, "config_patch", {"kind": "policy",
                 "changes": {"daily_model_cycles": 42}, "expected_version": prior["version"]})
    assert service.control("synthetic-bridge-secret", "admin", "config_get", {"kind": "policy"}) == first
    with pytest.raises(AssistantError, match="修改"):
        service.control("synthetic-bridge-secret", "admin", "config_patch", {"kind": "policy", "changes": {}, "expected_version": prior["version"]})
    with pytest.raises(AssistantError):
        service.control("synthetic-bridge-secret", "admin", "assistant_start", {})
    with pytest.raises(AssistantError):
        service.control("synthetic-bridge-secret", "admin", "config_get", {"kind": "persona"})


@pytest.mark.asyncio
async def test_priority_checkpoint_drains_local_message_once(pg_store):
    service = prepared(pg_store)
    token = service.sessions.login(PASSWORD, "role")
    service.control("synthetic-bridge-secret", "control", "assistant_start", {"background": False})
    received = service.local.post(token, "synthetic-priority", "1" * 32)
    await service.drain_local()
    await service.drain_local()
    event = Event.from_dict(events.get(pg_store, received["id"])["event"])
    assert (await service.core.handle(event))["duplicate"]
    with pg_store.db() as db:
        assert db.execute("SELECT count(*) FROM core_actions WHERE status='succeeded'").fetchone()[0] == 1
    service.core.stop()
    service.control("synthetic-bridge-secret", "control", "assistant_start", {})
    with pytest.raises(AssistantError):
        service.local.validate_event(event)


def test_start_cannot_extend_deadline_or_change_mode_implicitly(pg_store):
    service = prepared(pg_store)
    args = {"duration_minutes": 10, "background": False}
    original = service.control("synthetic-bridge-secret", "control", "assistant_start", args)["run"]
    again = service.control("synthetic-bridge-secret", "control", "assistant_start", {**args, "duration_minutes": 1440})
    assert again["run"] == original and again["already_running"]
    for invalid in ({"read_only": "false"}, {"background": "false"}, {"background": True}):
        with pytest.raises(AssistantError):
            service.control("synthetic-bridge-secret", "control", "assistant_start", invalid)
    assert pg_store.state("core-run") == original


def test_restore_preview_suppresses_heartbeat_and_releases_on_error(pg_store):
    service = prepared(pg_store)
    token = service.sessions.login(PASSWORD, "admin")
    request = service.sessions.request(token, "admin")
    management = Management(service)
    pg_store.backup(force=True)
    name = management.backups(request)[0]["name"]
    preview = management.restore_preview(request, name)
    assert not service.heartbeat()
    assert management.restore(request, preview["token"], PASSWORD)["restored"]
    assert service.heartbeat()
    token = service.sessions.login(PASSWORD, "admin")
    request = service.sessions.request(token, "admin")
    with pytest.raises(AssistantError):
        management.restore_preview(request, "absent.ccbackup")
    assert service.maintenance_until == 0


def test_yaml_import_preview_is_scoped_and_preserves_cas_and_policy(pg_store):
    import yaml

    service = prepared(pg_store)
    token = service.sessions.login(PASSWORD, "admin")
    request = service.sessions.request(token, "admin")
    management = Management(service)
    current = pg_store.read("persona")
    policy = service.admin.repository.policy("persona/@config")
    value = {**current["data"], "name": "synthetic-import"}
    preview = management.operation(request, "yaml_preview", {"kind": "persona", "id": None,
        "text": yaml.safe_dump(value, allow_unicode=True), "expected_version": current["version"]})
    assert not preview["written"] and preview["data"] == value
    assert pg_store.read("persona") == current
    role = service.sessions.request(service.sessions.login(PASSWORD, "role"), "role")
    with pytest.raises(AssistantError):
        management.import_preview(role, "persona", None, yaml.safe_dump(value), current["version"])
    written = management.correct(request, "persona", None, preview["data"], preview["expected_version"])
    assert written["data"] == value
    updated = service.admin.repository.policy("persona/@config")
    assert updated.classification == policy.classification and updated.owners == policy.owners
    with pytest.raises(AssistantError):
        management.correct(request, "persona", None, current["data"], preview["expected_version"])
