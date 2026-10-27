import json

import pytest

from companion_core.errors import AssistantError
from companion_core.operation_context import Trace, bounded
from companion_service.service import Service
from tests_postgres.test_runtime import Model
from tests_postgres.test_web import ADMIN, PASSWORD, Host, client


@pytest.fixture
def fresh(pg_store):
    return Service(pg_store, Model(), Host(), bridge_secret="synthetic-bridge-secret")


def test_plain_home_and_no_ticket_submission_are_actionable(fresh):
    admin = client(fresh, "admin")
    page = admin.get("/").text
    assert '<form id="setup" hidden><fieldset disabled>' in page and 'id="setup-guidance"' in page
    response = admin.post("/setup", json={"password": PASSWORD}, headers={"origin": ADMIN})
    assert response.json()["error"] == "setup_ticket_missing"
    assert "service_setup" in response.json()["message"]
    assert not fresh.store.state("web-owner")
    response = admin.post("/setup", json={"ticket": "unissued-synthetic-token", "password": PASSWORD}, headers={"origin": ADMIN})
    assert response.json()["error"] == "setup_ticket_unissued"
    assert '<form id="setup"' not in client(fresh, "role").get("/").text


def test_validate_never_consumes_and_setup_rejects_reuse(fresh):
    admin = client(fresh, "admin")
    headers = {"origin": ADMIN}
    ticket = fresh.sessions.setup_ticket()
    for _ in range(2):
        assert admin.post("/setup/validate", json={"ticket": ticket}, headers=headers).json()["valid"]
        assert not fresh.store.state("web-setup")["consumed"]
    assert admin.post("/setup/validate", json={"ticket": ticket}).status_code == 403
    assert admin.post("/setup", json={"ticket": ticket, "password": PASSWORD}, headers=headers).status_code == 200
    assert fresh.status()["status"] == "paused"
    for path in ("/setup", "/setup/validate"):
        response = admin.post(path, json={"ticket": ticket, "password": PASSWORD}, headers=headers)
        assert response.json()["error"] == "setup_ticket_used"


def test_expired_invalid_and_superseded_tickets(fresh):
    ticket = fresh.sessions.setup_ticket()
    with pytest.raises(AssistantError) as error:
        fresh.sessions.configure("different-synthetic-token", PASSWORD)
    assert error.value.code == "setup_ticket_invalid"
    fresh.store.set_state("web-setup", fresh.store.state("web-setup") | {"expires": 0})
    with pytest.raises(AssistantError) as error:
        fresh.sessions.configure(ticket, PASSWORD)
    assert error.value.code == "setup_ticket_expired"
    replacement = fresh.sessions.setup_ticket()
    with pytest.raises(AssistantError) as error:
        fresh.sessions.configure(ticket, PASSWORD)
    assert error.value.code == "setup_ticket_invalid"
    assert fresh.sessions.configure(replacement, PASSWORD)["configured"]


def test_expiry_is_rechecked_after_password_hash(fresh, monkeypatch):
    from companion_core import sessions

    ticket = fresh.sessions.setup_ticket()
    original = sessions.password_hash

    def slow_hash(*args):
        result = original(*args)
        fresh.store.set_state("web-setup", fresh.store.state("web-setup") | {"expires": 0})
        return result

    monkeypatch.setattr(sessions, "password_hash", slow_hash)
    with pytest.raises(AssistantError) as error:
        fresh.sessions.configure(ticket, PASSWORD)
    assert error.value.code == "setup_ticket_expired" and not fresh.store.state("web-owner")


def test_late_host_verification_never_binds(fresh, monkeypatch):
    # Verification completed after the deadline cannot mutate the management binding.
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    monkeypatch.setattr(fresh, "control_verifier", lambda _: now.__setitem__(0, now[0] + 30))
    with Trace("test", emit=False).activate(), pytest.raises(AssistantError) as error:
        fresh.control("synthetic-bridge-secret", "admin", "management_open", {})
    assert error.value.code == "control_timeout"
    assert not fresh.store.state("service-admin-thread")


def test_control_failure_exposes_safe_stages_without_identity(fresh, monkeypatch):
    def unavailable(_):
        raise AssistantError("host_timeout", "Unable to verify host")

    monkeypatch.setattr(fresh, "control_verifier", unavailable)
    response = client(fresh, "admin").post("/control", json={"thread": "private-chat-identity",
        "operation": "management_open", "arguments": {}}, headers={"authorization": "Bearer synthetic-bridge-secret"})
    assert response.status_code == 403
    data = response.json()
    assert data["error"] == "host_timeout"
    assert any(row["stage"] == "host_verification" for row in data["diagnostics"]["stages"])
    assert "private-chat-identity" not in json.dumps(data)
    assert not fresh.store.state("service-admin-thread")


def test_expired_control_budget_fails_before_binding(fresh):
    with pytest.raises(AssistantError):
        with bounded(0):
            fresh.control("synthetic-bridge-secret", "admin", "management_open", {})
    assert not fresh.store.state("service-admin-thread")
