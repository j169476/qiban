import hashlib
import secrets
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image

from companion_core.asset_transfer import capture
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import AdminService
from companion_core.profiles import default_profile
from companion_core.public_workspace import PublicWorkspace
from companion_core.runtime import Companion
from companion_core.task_contracts import SCHEMAS
from companion_core.tasks import ExplicitTasks
from companion_service.task_runner import TaskRunner
from tests_core.test_task_learning import QUESTIONS
from tests_postgres.test_runtime import Adapter, Host


@pytest.fixture
def pg_profile():
    profile = default_profile()
    profile.schemas.update(SCHEMAS)
    return profile


class NativeModel:
    def __init__(self, folder):
        self.folder, self.calls = folder, []
        self.forged = False

    async def generate(self, instructions, context, schema, images=(), **options):
        self.calls.append((context, options.get("native")))
        if options.get("native") == "web":
            return {"result": {"status": "done", "reason": "", "image_path": "", "sources": [{
                "url": "https://example.org/source", "title": "synthetic", "excerpt": "material with conditions",
                "note": "learned note", "tags": []}]}, "evidence": {"web_calls": 1, "thread_id": "synthetic",
                "opened": [] if self.forged else [{"url": "https://example.org/source", "reference": "open-1"}]}}
        if options.get("native") == "image":
            path = self.folder / "generated.png"
            Image.new("RGB", (32, 32), "blue").save(path)
            return {"result": {"status": "done", "reason": "", "sources": [], "image_path": str(path)},
                    "evidence": {"isolated_image": {"path": str(path), "sha256": "forged" if self.forged else
                                                       hashlib.sha256(path.read_bytes()).hexdigest()}}}
        if "questions" in schema["properties"]:
            questions = deepcopy(QUESTIONS)
            for question in questions:
                question["evidence"][0]["source_id"] = context["sources"][0]["id"]
            return {"questions": questions}
        if "answers" in schema["properties"]:
            assert "withheld-" not in str(context)
            return {"answers": [{"id": q["id"], "answer": "synthetic"} for q in context["questions"]]}
        return {"grades": [{"id": q["id"], "passed": True, "reason": "synthetic", "gap": ""}
                           for q in context["questions"]], "limitations": "model self check"}


def setup(store, folder):
    admin, model = AdminService(store), NativeModel(folder)
    adapter = Adapter()
    core = Companion(store, model, Host(), [adapter])
    core.start()
    actor = ActorRef("local", "owner", "web")

    def authenticate(actor, conversation, session):
        return admin.authority.issue(actor=actor, conversation=conversation, audience="private", recipients=(actor,))

    request = authenticate(actor, "private-local", "")
    service = SimpleNamespace(core=core, store=store, admin=admin)
    manager = ExplicitTasks(core, admin.authority, authenticate=authenticate,
                            session_active=lambda _: True, handler=TaskRunner(service))
    return manager, request, actor, model, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["web", "image", "learn"])
async def test_real_private_workflow_with_stub_native_tools(pg_store, tmp_path, kind):
    manager, request, actor, model, adapter = setup(pg_store, tmp_path)
    arguments = ({"objective": "learn conditions", "criteria": ["apply"], "round_limit": 2}
                 if kind == "learn" else {"prompt": "private request"})
    identifier = manager.submit(request, actor, kind, arguments, secrets.token_hex(16))["id"]
    for _ in range(4):
        await manager.step()
        if manager._state(identifier)["status"] != "queued":
            break
    result = manager.detail(request, identifier)
    assert result["status"] == "completed", result
    assert len(model.calls) == (4 if kind == "learn" else 1)
    assert not adapter.sent
    assert not PublicWorkspace(pg_store).records("task_results")[0]
    assert not PublicWorkspace(pg_store).records("memories")[0]
    assert len(pg_store.records("memories")[0]) == 1
    if kind == "image":
        assert len(capture(pg_store)) == 1
        assert "generated.png" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["web", "image"])
async def test_fabricated_native_evidence_never_becomes_a_task_result(pg_store, tmp_path, kind):
    manager, request, actor, model, adapter = setup(pg_store, tmp_path)
    model.forged = True
    identifier = manager.submit(request, actor, kind, {"prompt": "test"}, secrets.token_hex(16))["id"]
    await manager.step()
    assert manager._state(identifier)["status"] == "failed"
    assert not pg_store.records("task_results")[0]
    assert not pg_store.records("task_assets")[0]
    assert not adapter.sent


@pytest.mark.asyncio
async def test_native_daily_budget_is_shared_and_never_restarts_role(pg_store, tmp_path):
    manager, request, actor, model, _ = setup(pg_store, tmp_path)
    for _ in range(2):
        identifier = manager.submit(request, actor, "image", {"prompt": "test"}, secrets.token_hex(16))["id"]
        await manager.step()
    assert manager._state(identifier)["error"] == "native_quota"
    assert len(model.calls) == 1
    manager.core.stop()
    with pytest.raises(AssistantError):
        manager.submit(request, actor, "web", {"prompt": "test"}, secrets.token_hex(16))
