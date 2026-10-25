import json
import secrets
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bili_assistant.connector import BilibiliConnector
from bili_assistant.engine import Engine
from bili_assistant.profiles import profile
from companion_core import baseline
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import AdminService, GovernanceRepository, GovernedContext
from companion_core.postgres_transfer import create, fingerprint, restore
from companion_core.public_workspace import PublicWorkspace
from companion_core.runtime import Companion
from companion_core.sessions import SessionService
from companion_service.admin import Management
from tests_postgres.test_bili_governance import NOW, Model, Platform, patch
from tests_postgres.test_runtime import Adapter, Host
from tests_postgres.test_seed_import import import_seed
from tests_postgres.test_task_runner import setup as task_setup


@pytest.fixture
def pg_profile(request):
    return None if getattr(request, "param", "bili") is None else profile()


def management(store):
    admin = AdminService(store)
    owner = ActorRef("local", "owner", "web")
    request = admin.authority.issue(actor=owner, conversation="admin", audience="private", recipients=(owner,),
                                    purpose="management", administrator=True)
    return Management(SimpleNamespace(store=store, admin=admin)), request


def register(store):
    ids = ["foundation-" + str(i) for i in range(3)]
    for identifier in ids:
        store.write("memories", {"schema_version": 1, "id": identifier, "kind": "user_preference",
            "content": identifier + "：公开固定设定。" * 300, "confidence": "explicit", "tags": [], "locked": False,
            "source": {"type": "user_instruction", "recorded_at": NOW.isoformat(), "reference": "manual"}},
            None, "fixture", identifier)
    patch(store, "persona", core={"description": "shared-persona-canary", "speaking_style": ["自然交流"]},
          foundation_memories=ids)
    patch(store, "companion", context_char_limit=4000, context_record_limit=6)
    expected = baseline.preview(store)
    baseline.publish(store, expected, "explicit-user")
    return expected


@pytest.mark.parametrize("pg_profile", [None], indirect=True)
def test_publication_keeps_exact_content_and_never_claims_it_private_on_first_setup(pg_store, tmp_path):
    import_seed(pg_store, tmp_path)
    expected = baseline.preview(pg_store)
    before = {(p["kind"], p["id"]): pg_store.read(p["kind"], p["id"]) for p in expected["pins"]}
    baseline.publish(pg_store, expected, "explicit-user")
    admin = AdminService(pg_store)
    sessions = SessionService(admin.repository, admin.authority)
    sessions.configure(sessions.setup_ticket(), "synthetic-baseline-password")
    for key, value in before.items():
        assert pg_store.read(*key) == value
    assert admin.repository.policy("persona/@config").classification == "public"
    assert admin.repository.policy("memories/foundation").classification == "public"
    assert not pg_store.state("seed-owner-claim")["pending"]
    assert pg_store.state("run")["status"] == "paused"
    with pg_store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM governance_grants").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_same_complete_baseline_public_and_private_with_learning_and_isolation(pg_store):
    expected = register(pg_store)
    patch(pg_store, "manifest", account_uid="123")
    patch(pg_store, "policy", min_action_interval_seconds=0)
    patch(pg_store, "companion", memory_maintenance=False, daily_reflection=False, knowledge_review=False,
          media_fallback="off", search_expansion=False, native_tools_enabled=False)

    class Growing(Model):
        async def generate(self, instructions, context, schema, images=(), **options):
            if "queries" in schema["properties"]:
                return {"queries": []}
            value = await super().generate(instructions, context, schema, images, **options)
            if "event" in context:
                value["relationship"] = {"delta": 1, "impression": "一起聊天", "facts": ["喜欢讨论公开材料"]}
            return value

    platform, model, host = Platform(), Growing(), Host()
    core = Companion(pg_store, model, host, [BilibiliConnector(Engine(pg_store, host, platform, lambda: NOW))], clock=lambda: NOW)
    core.start()
    try:
        await core.cycle()
        assert len(platform.sent) == len(model.calls) == 3
        for context in model.calls:
            assert context["persona"] == pg_store.read("persona")["data"]
            assert len(context["foundation_memories"]) == 3
            assert len(json.dumps(context["foundation_memories"], ensure_ascii=False)) > 4000
            assert all(item == pg_store.read("memories", item["data"]["id"]) for item in context["foundation_memories"])
            assert not any(item["data"]["id"].startswith("foundation-") for item in context["memory"]["records"])
            serialized = json.dumps(context, ensure_ascii=False)
            if "event" in context:
                own = context["event"]["actor"]["user_id"]
                assert "private-canary-" + own in serialized
                assert "private-canary-" + ("22" if own == "11" else "11") not in serialized
            else:
                assert "private-canary" not in serialized
        learned = [item for item in pg_store.records("memories")[0]
                   if not item["data"]["id"].startswith("foundation-")]
        assert len(learned) == 3
        assert {pg_store.read("relationships", uid)["data"]["affinity"] for uid in ("11", "22")} == {51}
        for pin in expected["pins"]:
            assert pg_store.version(pin["kind"], pin["id"]) == pin["version"]
        assert {r["data"]["content"] for r in PublicWorkspace(pg_store).records("memories")[0]
                if not r["data"]["id"].startswith("foundation-")} == {"public-memory"}
    finally:
        core.stop()


@pytest.mark.parametrize("kind,identifier", [("persona", None), ("memories", "foundation-0")])
def test_actor_labels_and_allow_locked_cannot_bypass_manual_boundary(pg_store, kind, identifier):
    register(pg_store)
    item = pg_store.read(kind, identifier)
    for actor in ("reflection", "admin:forged", "explicit-user"):
        with pytest.raises(AssistantError) as error:
            pg_store.write(kind, item["data"], item["version"], actor, identifier, allow_locked=True)
        assert error.value.code == "baseline_locked"
    assert pg_store.read(kind, identifier) == item


def test_manual_add_edit_remove_and_failed_edit_rollback(pg_store):
    register(pg_store)
    manager, request = management(pg_store)
    card = pg_store.read("persona")
    manager.operation(request, "foundation_add", {"id": "manual-new", "content": "新增共用设定",
                                                  "expected_version": card["version"]})
    item = pg_store.read("memories", "manual-new")
    assert item["data"]["locked"]
    manager.correct(request, "memories", "manual-new", {**item["data"], "content": "手动更正"}, item["version"])
    changed = pg_store.read("memories", "manual-new")
    with pytest.raises(AssistantError) as error:
        manager.correct(request, "memories", "manual-new", {**changed["data"], "source": {
            **changed["data"]["source"], "type": "interaction"}}, changed["version"])
    assert error.value.code == "baseline_source"
    assert pg_store.read("memories", "manual-new") == changed
    # Even an authenticated edit rolls back atomically if the complete baseline is too large.
    with pytest.raises(AssistantError):
        manager.correct(request, "memories", "manual-new", {**changed["data"], "content": "x" * baseline.MAX_CHARS},
                        changed["version"])
    assert pg_store.read("memories", "manual-new") == changed
    old_bundle = baseline.load(pg_store)
    card = pg_store.read("persona")
    manager.admin.patch_config(request, "persona", {"foundation_memories": card["data"]["foundation_memories"][:-1]},
                               card["version"])
    with pytest.raises(AssistantError):
        old_bundle.check()
    assert pg_store.exists("memories", "manual-new")
    assert not manager.admin.repository.policy("memories/manual-new").valid
    assert "manual-new" not in str(PublicWorkspace(pg_store).context("手动更正"))
    assert len(baseline.load(pg_store).context["foundation_memories"]) == 3


@pytest.mark.parametrize("change", ["content", "policy", "active"])
def test_publish_requires_the_exact_reviewed_revisions_and_paused_run(pg_store, change):
    expected = baseline.preview(pg_store)
    if change == "content":
        patch(pg_store, "persona", name="changed")
    elif change == "policy":
        repository = GovernanceRepository(pg_store)
        old = repository.policy("persona/@config")
        repository.set_policy(replace(old, version="changed"), old.version, "fixture")
    else:
        pg_store.set_state("core-run", {"status": "active"})
    with pytest.raises(AssistantError):
        baseline.publish(pg_store, expected, "explicit-user")
    assert not baseline.enabled(pg_store)


def test_unavailable_baseline_never_silently_switches_persona_and_denies_role_admin(pg_store):
    register(pg_store)
    manager, request = management(pg_store)
    role = manager.admin.authority.issue(actor=ActorRef("local", "other"), conversation="other", audience="private",
                                          recipients=(ActorRef("local", "other"),))
    with pytest.raises(AssistantError):
        manager.operation(role, "baseline_publish", {"expected": baseline.preview(pg_store)})
    bundle = baseline.load(pg_store)
    old = manager.admin.repository.policy("persona/@config")
    manager.admin.classify(request, resource=old.resource, expected=old.version, classification="restricted")
    with pytest.raises(AssistantError):
        bundle.check()
    with pytest.raises(AssistantError):
        PublicWorkspace(pg_store).read("persona")
    event = Event("alpha", "bot", "message", ActorRef("alpha", "a"), "private-a", NOW.isoformat(), "test", "private")
    with pytest.raises(AssistantError):
        GovernedContext(pg_store, event).persona_context()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "revoke"])
async def test_baseline_change_before_send_cancels_without_bypassing_provenance(pg_store, change):
    register(pg_store)
    manager, request = management(pg_store)

    class Reply:
        async def generate(self, instructions, context, schema, *args, **kwargs):
            if "queries" in schema["properties"]:
                return {"queries": []}
            return {"note": "", "action": {"capability": "message.reply", "arguments": {"text": "synthetic"}},
                    "memories": ["learned experience"], "identity_proposals": [], "relationship": None}

    adapter = Adapter()
    core = Companion(pg_store, Reply(), Host(), [adapter])
    core.start()

    def invalidate():
        if change == "content":
            item = pg_store.read("memories", "foundation-0")
            manager.correct(request, "memories", "foundation-0", {**item["data"], "content": "corrected"}, item["version"])
        else:
            old = manager.admin.repository.policy("memories/foundation-0")
            manager.admin.classify(request, resource=old.resource, expected=old.version, classification="restricted")

    adapter.before = invalidate
    try:
        event = Event("alpha", "bot", "message", ActorRef("alpha", "a"), "private-a", NOW.isoformat(), "test", "private")
        result = await core.handle(event)
        assert result["action"]["status"] == "cancelled"
        assert not adapter.sent
    finally:
        core.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["web", "image", "learn"])
async def test_explicit_tasks_use_baseline_and_keep_learning_private(pg_store, tmp_path, kind):
    register(pg_store)
    manager, request, actor, model, _ = task_setup(pg_store, tmp_path)
    arguments = ({"objective": "learn conditions", "criteria": ["apply"], "round_limit": 2}
                 if kind == "learn" else {"prompt": "private request"})
    identifier = manager.submit(request, actor, kind, arguments, secrets.token_hex(16))["id"]
    try:
        for _ in range(4):
            await manager.step()
            if manager._state(identifier)["status"] != "queued":
                break
        assert manager._state(identifier)["status"] == "completed", manager._state(identifier)
        for context, _ in model.calls:
            assert context["persona"] == pg_store.read("persona")["data"]
            assert len(context["foundation_memories"]) == 3
        assert not PublicWorkspace(pg_store).records("task_results")[0]
        assert len(pg_store.records("memories")[0]) == 4
        latest = manager._state(identifier)["latest"]
        assert "memories/foundation-0" in GovernanceRepository(pg_store).policy("task_results/" + latest).sources
    finally:
        manager.core.stop()


def test_backup_restore_preserves_shared_manual_settings_and_pauses(pg_store):
    register(pg_store)
    before = deepcopy(baseline.load(pg_store).context)
    key = secrets.token_bytes(32)
    blob = create(pg_store, key)
    manager, request = management(pg_store)
    item = pg_store.read("memories", "foundation-0")
    manager.correct(request, "memories", "foundation-0", {**item["data"], "content": "corrected"}, item["version"])
    restore(pg_store, blob, key, expected=fingerprint(pg_store))
    assert baseline.load(pg_store).context == before
    item = pg_store.read("memories", "foundation-0")
    with pytest.raises(AssistantError):
        pg_store.write("memories", item["data"], item["version"], "reflection", "foundation-0", allow_locked=True)
    assert (pg_store.state("core-run") or {}).get("status", "paused") == "paused"


def test_authenticated_web_can_edit_baseline_but_role_cannot_manage_it(pg_store):
    from companion_service.service import Service
    from tests_postgres.test_runtime import Model as CoreModel
    from tests_postgres.test_web import PASSWORD, Host, client, login

    register(pg_store)
    service = Service(pg_store, CoreModel(), Host(), bridge_secret="synthetic-bridge-secret")
    service.sessions.configure(service.sessions.setup_ticket(), PASSWORD)
    admin, role = client(service, "admin"), client(service, "role")
    headers, role_headers = login(admin, service, "admin"), login(role, service, "role")
    page = admin.get("/")
    assert 'id="baseline-load"' in page.text and 'id="foundation-add"' in page.text
    assert 'id="foundation-add"' not in role.get("/").text
    body = {"operation": "baseline_preview", "arguments": {}}
    assert admin.post("/operate", json=body).status_code == 403
    assert role.post("/operate", json=body, headers=role_headers).status_code == 404
    preview = admin.post("/operate", json=body, headers=headers).json()
    assert preview["shared"] and preview["foundation_count"] == 3
    before = pg_store.read("persona")
    added = admin.post("/operate", json={"operation": "foundation_add", "arguments": {
        "id": "web-fixed", "content": "网页手动添加", "expected_version": before["version"]}}, headers=headers)
    assert added.status_code == 200, added.text
    assert len(baseline.load(pg_store).context["foundation_memories"]) == 4
    assert service.status()["status"] == "paused"
