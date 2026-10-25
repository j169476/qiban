import secrets
from dataclasses import replace

import pytest

from companion_core.access import RecordPolicy
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import AdminService
from companion_core.profiles import default_profile
from companion_core.runtime import Companion
from companion_core.task_contracts import SCHEMAS
from companion_core.tasks import ExplicitTasks
from tests_postgres.test_runtime import Adapter, Host, Model


@pytest.fixture
def pg_profile():
    profile = default_profile()
    profile.schemas.update(SCHEMAS)
    return profile


@pytest.fixture
def tasks(pg_store):
    admin = AdminService(pg_store)
    core = Companion(pg_store, Model(), Host(), [Adapter()])
    actor = ActorRef("local", "owner", "web")
    active = [True]

    def auth(actor, conversation, session):
        if not active[0]:
            raise AssistantError("session_expired", "synthetic revoked session")
        return admin.authority.issue(actor=actor, conversation=conversation, audience="private", recipients=(actor,))

    calls = []

    async def handler(task, previous, *, request, guard):
        guard()
        calls.append(task["arguments"])
        return {"phase": "done", "complete": True, "payload": {"note": "private-result"}, "summary": "private-result"}

    value = ExplicitTasks(core, admin.authority, authenticate=auth, session_active=lambda _: active[0], handler=handler)
    return value, auth(actor, "private-local", "session"), actor, calls, active


def submit(tasks):
    manager, request, actor, _, _ = tasks
    manager.core.start()
    return manager.submit(request, actor, "web", {"prompt": "private request"}, secrets.token_hex(16))["id"]


@pytest.mark.asyncio
async def test_task_requires_explicit_run_and_duplicates_do_not_execute_twice(tasks):
    manager, request, actor, calls, _ = tasks
    nonce = secrets.token_hex(16)
    with pytest.raises(AssistantError):
        manager.submit(request, actor, "web", {"prompt": "private request"}, nonce)
    assert manager.listing(request) == []
    manager.core.start()
    identifier = manager.submit(request, actor, "web", {"prompt": "private request"}, nonce)["id"]
    assert manager.submit(request, actor, "web", {"prompt": "private request"}, nonce)["duplicate"]
    assert await manager.step()
    assert not await manager.step()
    assert len(calls) == 1
    result = manager.detail(request, identifier)
    assert result["status"] == "completed" and result["result"]["summary"] == "private-result"
    assert not manager.core.platforms.all()[0].sent


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["source", "session", "archive", "cancel", "configuration"])
async def test_inflight_task_guards_fail_closed(tasks, boundary):
    manager, request, actor, _, active = tasks
    identifier = submit(tasks)

    async def handler(task, previous, *, request, guard):
        if boundary == "source":
            root = manager.repository.policy("task_requests/" + identifier)
            manager.repository.set_policy(replace(root, version="revoked", valid=False), root.version, "test")
        elif boundary == "session":
            active[0] = False
        elif boundary == "archive":
            manager.core.host.archived = True
        elif boundary == "configuration":
            old = manager.store.read("policy")
            manager.store.write("policy", {**old["data"], "daily_model_cycles": 1}, old["version"], "test")
        else:
            manager.control(request, identifier, "cancel", manager._state(identifier)["revision"])
        return {"phase": "done", "complete": True, "payload": {}, "summary": "must not commit"}

    manager.handler = handler
    await manager.step()
    assert manager._state(identifier)["status"] in {"failed", "cancelled"}
    assert not manager.store.records("task_results")[0]


@pytest.mark.asyncio
async def test_stage_chain_has_full_provenance_and_cannot_enter_public_workspace(tasks):
    from companion_core.public_workspace import PublicWorkspace

    manager, request, _, _, _ = tasks
    identifier = submit(tasks)

    async def handler(task, previous, *, request, guard):
        return {"phase": "next", "complete": bool(previous), "payload": {"note": "private-phase"}}

    manager.handler = handler
    assert await manager.step() and await manager.step()
    detail = manager.detail(request, identifier)
    root = manager.repository.policy("task_requests/" + identifier)
    lease = manager.access.lease(request, ["task_results/" + detail["result"]["id"]])
    assert len(lease.versions) == 3
    assert not PublicWorkspace(manager.store).records("task_results")[0]
    manager.repository.set_policy(replace(root, version="corrected"), root.version, "test")
    with pytest.raises(AssistantError):
        manager.access.revalidate(lease)


@pytest.mark.asyncio
async def test_hidden_extra_material_and_old_control_revision_are_rejected(tasks):
    manager, request, _, _, _ = tasks
    identifier = submit(tasks)
    initial = manager._state(identifier)["revision"]
    manager.repository.set_policy(RecordPolicy("hidden", "v1", "secret"), None, "test")

    async def handler(task, previous, *, request, guard):
        return {"phase": "done", "complete": True, "payload": {}, "source_pins": [("hidden", "v1")]}

    manager.handler = handler
    await manager.step()
    assert manager._state(identifier)["status"] == "failed"
    assert not manager.store.records("task_results")[0]
    with pytest.raises(AssistantError):
        manager.control(request, identifier, "retry", initial)
