import secrets

import pytest
from starlette.testclient import TestClient

from companion_core import events
from companion_core.contracts import Event
from companion_core.errors import AssistantError
from companion_service.service import Service
from companion_service.web import create_app
from tests_postgres.test_runtime import Model

ADMIN = "http://127.0.0.1:45180"
ROLE = "http://127.0.0.1:45181"
PASSWORD = "synthetic-password-2026"


class Host:
    archived = False
    thread_id = "control"

    def identity(self):
        return self.thread_id

    def require_active(self, thread_id=None):
        if self.archived or (thread_id and thread_id not in {"control", "admin"}):
            raise AssistantError("chat_archived", "synthetic invalid chat")


@pytest.fixture
def service(pg_store):
    value = Service(pg_store, Model(), Host(), bridge_secret="synthetic-bridge-secret")
    value.sessions.configure(value.sessions.setup_ticket(), PASSWORD)
    return value


def client(service, kind):
    origin, other = (ADMIN, ROLE) if kind == "admin" else (ROLE, ADMIN)
    return TestClient(create_app(service, kind=kind, origin=origin, counterpart=other), base_url=origin)


def login(client, service, kind):
    origin = ADMIN if kind == "admin" else ROLE
    reply = client.post("/login", json={"password": PASSWORD}, headers={"origin": origin})
    assert reply.status_code == 200, reply.text
    token = client.cookies["companion_" + kind]
    return {"origin": origin, "x-csrf-token": service.sessions.csrf(token, kind)}


def test_login_does_not_start_and_local_host_is_not_authentication(service):
    role = client(service, "role")
    assert role.get("/status").status_code == 401
    assert role.post("/login", json={"password": PASSWORD}).status_code == 403
    assert role.post("/login", json={"password": PASSWORD}, headers={"origin": ADMIN}).status_code == 403
    headers = login(role, service, "role")
    response = role.get("/")
    assert response.status_code == 200 and "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "HttpOnly" in role.post("/login", json={"password": PASSWORD}, headers=headers).headers["set-cookie"]
    assert role.get("/status").json()["status"] == "paused"
    assert role.post("/messages", json={"text": "hello", "nonce": secrets.token_hex(16)}, headers=headers).status_code == 403
    assert role.get("/records").status_code == 404
    assert role.get("/", headers={"host": "evil.example:45181"}).status_code == 400


def test_admin_cas_same_service_and_csrf(service):
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    listing = admin.get("/records?kind=policy").json()
    assert listing["records"][0]["id"] == "@config"
    record = admin.get("/records?kind=policy&id=").json()
    body = {"operation": "config_patch", "arguments": {"kind": "policy", "changes": {}, "expected_version": record["version"]}}
    assert admin.post("/operate", json=body, headers={"origin": ADMIN}).status_code == 403
    assert admin.post("/operate", json=body, headers=headers).status_code == 200
    body["arguments"]["expected_version"] = "stale"
    assert admin.post("/operate", json=body, headers=headers).status_code == 409
    assert admin.post("/operate", content="x" * 70000, headers=headers).status_code == 413


def test_bridge_rejects_role_cookie_and_archived_control(service):
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    args = {"thread": "control", "operation": "assistant_start"}
    assert admin.post("/control", json=args, headers=headers).status_code == 403
    secret = {"authorization": "Bearer synthetic-bridge-secret"}
    service.host.archived = True
    assert admin.post("/control", json=args, headers=secret).status_code == 403
    service.host.archived = False
    assert admin.post("/control", json=args, headers=secret).status_code == 200
    assert service.status()["status"] == "active"
    service.host.archived = True
    with pytest.raises(AssistantError):
        service.core.guard()
    service.host.archived = False
    assert service.status()["status"] == "paused"
    assert admin.get("/").status_code == 200
    assert service.status()["status"] == "paused"


@pytest.mark.asyncio
async def test_web_message_identity_session_revocation_and_durable_reply(service):
    role = client(service, "role")
    headers = login(role, service, "role")
    token = role.cookies["companion_role"]
    service.control("synthetic-bridge-secret", "control", "assistant_start", {})
    body = {"text": "<script>alert('not markup')</script>", "nonce": secrets.token_hex(16)}
    assert role.post("/messages", json={**body, "actor": "another-person"}, headers=headers).status_code == 403
    response = role.post("/messages", json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert role.post("/messages", json=body, headers=headers).json()["duplicate"]
    event = Event.from_dict(events.get(service.store, response.json()["id"])["event"])
    result = await service.core.handle(event)
    assert result["action"]["status"] == "succeeded"
    history = role.get("/messages").json()
    assert [item["speaker"] for item in history] == ["user", "assistant"]
    assert history[0]["text"] == body["text"]
    service.sessions.logout(token)
    with pytest.raises(AssistantError, match="会话"):
        service.local.validate_event(event)
    assert role.get("/messages").status_code == 401


@pytest.mark.asyncio
async def test_restart_does_not_resume_or_replay(service):
    service.control("synthetic-bridge-secret", "control", "assistant_start", {})
    await service.open()
    assert service.status()["status"] == "paused"
    await service.close()
    assert service.status()["status"] == "paused"


def test_authenticated_backup_restore_requires_preview_password_and_invalidates_session(service):
    admin = client(service, "admin")
    headers = login(admin, service, "admin")

    def operation(name, args=None):
        return admin.post("/operate", json={"operation": name, "arguments": args or {}}, headers=headers)

    assert operation("backup").status_code == 200
    files = operation("backup_list").json()
    assert len(files) == 1
    assert operation("restore_preview", {"name": "../other.ccbackup"}).status_code == 403
    preview = operation("restore_preview", {"name": files[0]["name"]}).json()
    assert preview["counts"]["content_records"] > 0
    restored = operation("restore", {"token": preview["token"], "password": PASSWORD})
    assert restored.status_code == 200, restored.text
    assert admin.get("/status").status_code == 401
    assert service.status()["status"] == "paused"
