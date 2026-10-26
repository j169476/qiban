"""Web management uses the real Bilibili lifecycle with a synthetic adapter."""
import pytest

from bili_assistant.connector import BilibiliConnector
from bili_assistant.engine import Engine
from bili_assistant.profiles import profile
from companion_core.errors import AssistantError
from companion_service.service import Service
from tests_postgres.test_bili_governance import NOW, Platform, patch
from tests_postgres.test_runtime import Model
from tests_postgres.test_web import PASSWORD, Host, client, login


@pytest.fixture
def pg_profile():
    return profile()


@pytest.fixture
def service(pg_store):
    patch(pg_store, "manifest", account_uid="123")
    host = Host()
    connector = BilibiliConnector(Engine(pg_store, host, Platform()))
    value = Service(pg_store, Model(), host, adapters=[connector], bridge_secret="synthetic-bridge-secret")
    value.sessions.configure(value.sessions.setup_ticket(), PASSWORD)
    value.control("synthetic-bridge-secret", "admin", "management_open", {})
    return value


def start_web(service, **options):
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    response = admin.post("/operate", json={"operation": "platform_control", "arguments": {
        "platform": "bilibili", "action": "start", "expected_generation": None, "options": options}}, headers=headers)
    assert response.status_code == 200, response.text
    return service.platform_runs.cores["bilibili"]


def test_web_admin_can_start_and_continue_without_changing_chat_mode(service):
    core = start_web(service)
    assert core.guard()["owner"] == "admin"
    with service.store.db() as db:
        assert db.execute("SELECT mode FROM sessions WHERE thread=?", ("admin",)).fetchone()["mode"] == "admin"
    assert service.store.state("run", {}).get("status") != "active"
    core.stop()
    # A previous web start does not grant direct role entry to the admin chat.
    with pytest.raises(AssistantError) as error:
        service.control("synthetic-bridge-secret", "admin", "bili_start", {})
    assert error.value.code == "mode_conflict"


@pytest.mark.parametrize("change,code", [("rebind", "mode_denied"), ("archive", "chat_archived")])
def test_web_run_rechecks_owner_and_host(service, change, code):
    core = start_web(service)
    if change == "rebind":
        service.store.set_state("service-admin-thread", "another-admin")
    else:
        service.host.archived = True
    with pytest.raises(AssistantError) as error:
        core.guard()
    assert error.value.code == code


def test_direct_core_start_cannot_bypass_web_authorization(service):
    core = service.platform_runs.cores["bilibili"]
    core.host.thread_id = "admin"
    with pytest.raises(AssistantError) as error:
        core.start()
    assert error.value.code == "mode_denied"


def test_role_chat_still_starts_without_management_permission(service):
    start_web(service).stop()
    service.control("synthetic-bridge-secret", "control", "bili_start", {})
    core = service.platform_runs.cores["bilibili"]
    assert core.guard()["owner"] == "control"
    assert core.host.management_owner is None


def test_failed_web_start_does_not_leave_management_authority(service, monkeypatch):
    core = service.platform_runs.cores["bilibili"]

    def fail(**kwargs):
        raise AssistantError("synthetic_failure", "synthetic startup failure")

    monkeypatch.setattr(core, "start", fail)
    with pytest.raises(AssistantError, match="synthetic startup failure"):
        service.manage_platform("bilibili", "start", None, {})
    assert core.host.thread_id is None
    assert core.host.management_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [55, 56, 62])
@pytest.mark.parametrize("read_only", [False, True])
async def test_web_run_polls_and_cycles_when_model_budget_is_exhausted(service, used, read_only):
    core = service.platform_runs.cores["bilibili"]
    core.clock = lambda: NOW
    adapter = core.platforms.all()[0]
    adapter.engine.clock = core.clock
    adapter.engine.adapter.incoming = []
    patch(service.store, "policy", daily_model_cycles=56)
    patch(service.store, "companion", memory_maintenance=False, daily_reflection=False,
          knowledge_review=False, media_fallback="off", search_expansion=False, native_tools_enabled=False)
    service.store.set_state("model-budget:2026-10-23", {"used": used, "sources": {}})
    core = start_web(service, read_only=read_only)

    assert await core.poll() == []
    report = await core.cycle()

    result = report["workflows"][0]
    stages = {item["name"]: item["status"] for item in result["stages"]}
    assert stages["inbox"] == "continue"
    assert stages["selection_context"] == ("stop" if used >= 56 else "continue")
    assert result["status"] == ("stopped" if used >= 56 else "completed")
    assert report["model_remaining"] == max(0, 56 - used)
    assert core.guard()["status"] == "active"
    assert service.store.state("model-budget:2026-10-23")["used"] == used
    assert not adapter.engine.adapter.sent
