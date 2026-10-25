import secrets
from dataclasses import replace

import pytest

from companion_core.public_workspace import PublicWorkspace
from companion_core.sessions import token_hash
from companion_service.service import Service
from tests_postgres.test_task_runner import NativeModel
from tests_postgres.test_web import ADMIN, PASSWORD, ROLE, Host, client, login


@pytest.fixture
def pg_profile(request):
    if getattr(request, "param", None) == "bili":
        from bili_assistant.profiles import profile

        return profile()


@pytest.fixture
def service(pg_store, tmp_path):
    value = Service(pg_store, NativeModel(tmp_path), Host(), bridge_secret="synthetic-bridge-secret")
    value.sessions.configure(value.sessions.setup_ticket(), PASSWORD)
    return value


def start(service):
    service.control("synthetic-bridge-secret", "control", "assistant_start", {"background": False})


def body(kind="web"):
    args = {"prompt": "synthetic private request"}
    if kind == "learn":
        args = {"objective": "conditions", "criteria": ["apply"], "round_limit": 2}
    if kind == "video":
        args = {"bvid": "BV1xx411c7mD", "page_index": 0, "start_seconds": 0, "end_seconds": 1200,
                "mode": "text", "reason": "specific gap"}
    return {"kind": kind, "arguments": args, "nonce": secrets.token_hex(16)}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["web", "image", "learn"])
async def test_web_task_to_real_core_workflow_and_private_result(service, kind):
    role = client(service, "role")
    headers = login(role, service, "role")
    value = body(kind)
    assert role.post("/tasks", json=value, headers=headers).status_code == 403
    start(service)
    posted = role.post("/tasks", json=value, headers=headers)
    assert posted.status_code == 202, posted.text
    identifier = posted.json()["id"]
    assert role.post("/tasks", json=value, headers=headers).json()["duplicate"]
    for _ in range(4):
        await service.tasks.step()
    detail = role.get("/tasks", params={"id": identifier}).json()
    assert detail["status"] == "completed", detail
    assert len(role.get("/tasks").json()) == 1
    assert len(service.core.decisions.model.calls) == (4 if kind == "learn" else 1)
    assert not PublicWorkspace(service.store).records("task_results")[0]
    assert not PublicWorkspace(service.store).records("memories")[0]
    if kind == "image":
        image = role.get("/task-image/" + identifier)
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        assert image.headers["cache-control"] == "no-store"
        assert client(service, "role").get("/task-image/" + identifier).status_code == 401
        admin = client(service, "admin")
        login(admin, service, "admin")
        forged = client(service, "role")
        forged.cookies.set("companion_role", admin.cookies["companion_admin"])
        assert forged.get("/task-image/" + identifier).status_code == 401
        root = "task_requests/" + identifier
        policy = service.admin.repository.policy(root)
        service.admin.repository.set_policy(replace(policy, version="revoked", valid=False), policy.version, "fixture")
        assert role.get("/task-image/" + identifier).status_code == 403
        assert role.get("/tasks").json() == []


def test_task_transport_rejects_forged_identity_csrf_origin_and_invalid_args(service):
    role = client(service, "role")
    headers = login(role, service, "role")
    start(service)
    value = body()
    assert role.post("/tasks", json=value, headers={"origin": ROLE}).status_code == 403
    assert role.post("/tasks", json=value, headers={**headers, "origin": ADMIN}).status_code == 403
    assert role.post("/tasks", json={**value, "actor": "somebody"}, headers=headers).status_code == 403
    assert role.post("/tasks", json={**value, "arguments": {"prompt": "x", "path": "secret"}}, headers=headers).status_code == 403
    admin = client(service, "admin")
    login(admin, service, "admin")
    assert admin.get("/tasks").status_code == 404
    assert not service.store.records("task_requests")[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "logout", "archive", "restart", "request_edit", "session_actor", "session_purpose"])
async def test_pending_task_cannot_survive_revocation_or_change(service, action):
    role = client(service, "role")
    headers = login(role, service, "role")
    start(service)
    identifier = role.post("/tasks", json=body(), headers=headers).json()["id"]
    detail = role.get("/tasks", params={"id": identifier}).json()
    if action == "cancel":
        args = {"id": identifier, "action": "cancel", "revision": "wrong"}
        assert role.post("/task-control", json=args, headers=headers).status_code == 409
        args["revision"] = detail["revision"]
        assert role.post("/task-control", json=args, headers=headers).status_code == 200
    elif action == "logout":
        assert role.post("/logout", json={}, headers=headers).status_code == 200
    elif action == "archive":
        service.host.archived = True
    elif action == "restart":
        await service.open()
        await service.close()
    elif action == "request_edit":
        record = service.store.read("task_requests", identifier)
        service.store.write("task_requests", {**record["data"], "arguments": {"prompt": "changed"}},
                            record["version"], "fixture", identifier)
    else:
        with service.store.db() as db:
            column = "actor" if action == "session_actor" else "purpose"
            value = "another" if action == "session_actor" else "admin"
            db.execute("UPDATE governance_sessions SET " + column + "=? WHERE id=?",
                       (value, token_hash(role.cookies["companion_role"])))
    await service.tasks.step()
    assert service.tasks._state(identifier)["status"] in {"failed", "cancelled"}
    assert not service.core.decisions.model.calls
    assert not service.store.records("task_results")[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "purpose_change"])
async def test_change_during_model_call_prevents_private_result_commit(service, operation):
    role = client(service, "role")
    headers = login(role, service, "role")
    start(service)
    identifier = role.post("/tasks", json=body(), headers=headers).json()["id"]
    original = service.tasks.handler

    async def handler(*args, **kwargs):
        value = await original(*args, **kwargs)
        if operation == "cancel":
            detail = role.get("/tasks", params={"id": identifier}).json()
            assert role.post("/task-control", json={"id": identifier, "action": "cancel",
                             "revision": detail["revision"]}, headers=headers).status_code == 200
        else:
            with service.store.db() as db:
                db.execute("UPDATE governance_sessions SET purpose='admin' WHERE id=?",
                           (token_hash(role.cookies["companion_role"]),))
        return value

    service.tasks.handler = handler
    await service.tasks.step()
    assert len(service.core.decisions.model.calls) == 1
    assert not service.store.records("task_results")[0]
    assert not service.store.records("memories")[0]


@pytest.mark.asyncio
async def test_task_image_backup_restore_keeps_provenance_and_requires_new_login(service):
    from companion_core.asset_transfer import _path
    from companion_core.postgres_transfer import create, fingerprint, restore

    role = client(service, "role")
    headers = login(role, service, "role")
    start(service)
    identifier = role.post("/tasks", json=body("image"), headers=headers).json()["id"]
    await service.tasks.step()
    original = role.get("/task-image/" + identifier).content
    service._pause("backup-test")
    key = secrets.token_bytes(32)
    backup = create(service.store, key)
    asset = service.store.records("task_assets")[0][0]["data"]
    path = _path(service.store, "assets/tasks/" + asset["filename"])
    path.unlink()
    restore(service.store, backup, key, expected=fingerprint(service.store))
    assert role.get("/task-image/" + identifier).status_code == 401
    login(role, service, "role")
    response = role.get("/task-image/" + identifier)
    assert response.status_code == 200 and response.content == original
    assert service.status()["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("pg_profile", ["bili"], indirect=True)
async def test_web_video_uses_bound_bili_reader_and_actual_window_progress(pg_store):
    from bili_assistant.connector import BilibiliConnector
    from bili_assistant.engine import Engine
    from tests_postgres.test_bili_governance import patch
    from tests_postgres.test_private_video_tasks import VideoModel, VideoPlatform

    patch(pg_store, "manifest", account_uid="123")
    patch(pg_store, "companion", media_max_seconds=600)
    host, platform = Host(), VideoPlatform()
    connector = BilibiliConnector(Engine(pg_store, host, platform))
    service = Service(pg_store, VideoModel(), host, adapters=[connector], bridge_secret="synthetic-bridge-secret")
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    role = client(service, "role")
    headers = login(role, service, "role")
    start(service)
    blocked = role.post("/tasks", json=body("video"), headers=headers).json()["id"]
    await service.tasks.step()
    assert role.get("/tasks", params={"id": blocked}).json()["error"] == "platform_not_running"
    assert not platform.windows
    service.control("synthetic-bridge-secret", "control", "bili_start", {})
    response = role.post("/tasks", json=body("video"), headers=headers)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    for _ in range(2):
        await service.tasks.step()
    detail = role.get("/tasks", params={"id": identifier}).json()
    assert detail["status"] == "completed", detail
    assert detail["result"]["payload"]["intervals"] == [[0, 1200]]
    assert platform.windows == [(0, 600), (600, 1200)] and not platform.sent
