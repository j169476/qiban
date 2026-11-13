"""D55: a cross-machine restore continues the source logical user's manual tasks.

The current Windows administrator keeps management, the app lock and every model and
platform check, while manual tasks are listed, read, submitted and retried as the
bound historical local user inside that user's own `desktop-manual:` conversation.
Model, B站 account and task stages are synthetic fakes: nothing here starts a real
model, account, network call or database server of its own.
"""
import json
import os
import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from keyring.backends.Windows import WinVaultKeyring

from companion_core import postgres_transfer
from companion_core.access import RecordPolicy
from companion_core.contracts import ActorRef, stable_key
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.postgres import PostgresStore
from companion_core.storage import utcnow
from qiban import desktop_identity
from qiban.desktop_identity import BINDING, ROLE
from qiban.desktop_management import LOCK, OWNER, DesktopManagement
from qiban.desktop_manual_tasks import QUEUE
from qiban.desktop_runtime import DesktopRuntime, provision_runtime
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_desktop_manual_tasks import ARGS, Bili
from tests_postgres.test_wechat_role import Model

__all__ = ["desktop_store", "service"]

OTHER_INSTALLATION = "installation-b"
OTHER_SID = "S-1-5-21-test-owner-b"
OTHER_PASSWORD = "another-strong-pass"
# The source installation's own manual conversation; the continuation must keep it.
SOURCE_CONVERSATION = "desktop-manual:installation-test"


def _connection():
    """The same disposable test installation as the desktop fixture; only the schema differs."""
    if not os.environ.get("QIBAN_DESKTOP_TEST_ROOT"):
        pytest.skip("Requires a disposable running desktop test installation")
    root = Path(os.environ["QIBAN_DESKTOP_TEST_ROOT"])
    state = json.loads((root / "installation.json").read_text(encoding="utf8"))
    vault = WinVaultKeyring()
    credential = json.loads(vault.get_password("qiban-desktop-probe-storage", state["installation"]))
    return {"host": "127.0.0.1", "port": state["port"], "user": "qiban_setup",
            "password": credential["bootstrap_password"], "dbname": "qiban"}


@pytest.fixture
def restored_store(tmp_path, service):
    """A second installation's database opened with the source key, as a portable restore must be."""
    options = _connection()
    schema = "desktop_manual_tasks_" + uuid.uuid4().hex
    with psycopg.connect(**options, autocommit=True) as db:
        db.execute("CREATE SCHEMA " + schema)
    store = PostgresStore(tmp_path / "restored", {**options, "options": "-c search_path=" + schema},
                          service.store._test_key, profile=service.store.profile)
    try:
        store.migrate()
        assert store.initialize()["ok"]
        yield store
    finally:
        store.database.close()
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute("DROP SCHEMA " + schema + " CASCADE")


def synthetic(summary):
    """A fake stage result; no model or platform read ever happens."""
    async def runner(task, previous, *, request, guard):
        guard()
        return {"phase": "done", "complete": True, "payload": {"text": summary}, "summary": summary}
    return runner


def _intruder(store, actor, conversation):
    """A private manual task written by another local identity; only that actor may read it."""
    identifier = stable_key("task", actor.key, secrets.token_hex(16))
    record = store.write("task_requests", {"schema_version": 1, "id": identifier, "actor": actor.as_dict(),
        "conversation": conversation, "kind": "video", "arguments": ARGS,
        "source": {"type": "user_instruction", "recorded_at": utcnow(), "reference": secrets.token_hex(16)},
        "locked": False}, None, "synthetic-intruder", identifier)
    store.set_state("explicit-task:" + identifier, {"status": "completed", "phase": "done", "step": 1,
        "latest": "", "request_version": record["version"], "generation": "", "owner": "", "session": "",
        "error": "", "attempts": 0, "revision": secrets.token_hex(16)})
    repository = GovernanceRepository(store)
    resource = "task_requests/" + identifier
    current = repository.policy(resource)
    repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "personal", frozenset((actor.key,)),
                                       conversation, "private", ()), current.version, "synthetic-intruder")
    return identifier


async def _restored_installation(service, store):
    """A writes its own manual tasks; its rows are restored, then B stages the adopted role and binding."""
    service.bili = Bili()
    runtime = DesktopRuntime(service, "manual-identity-a", model=Model())
    manual = runtime.manual
    manual.runner = synthetic("来源安装已完成的结果")
    completed = manual.submit(service.session_id, "video", ARGS, secrets.token_hex(16))["id"]
    await runtime.tasks.run(manual.step)
    waiting = manual.submit(service.session_id, "video", ARGS, secrets.token_hex(16))["id"]
    cancelled = manual.submit(service.session_id, "video", ARGS, secrets.token_hex(16))["id"]
    manual.control(service.session_id, cancelled, "cancel", manual.engine._state(cancelled)["revision"])
    assert service.store.state(QUEUE) == [waiting]
    source_owner = ActorRef(**service.store.state(OWNER)["actor"])
    source_role = ActorRef(**service.store.state(ROLE))
    blob = postgres_transfer.create(service.store, service.store._backup_key)
    try:
        runtime.close()
    finally:
        service.runtime = None
    assert postgres_transfer.restore(store, blob, service.store._backup_key,
                                     expected=postgres_transfer.fingerprint(store))["restored"]
    # The physical cutover, then the trusted staging step: the restored role becomes this role.
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        store.set_state(OWNER, {"format": 1, "installation": OTHER_INSTALLATION, "windows_sid": OTHER_SID,
                                "actor": ActorRef("local", uuid.uuid4().hex, OTHER_INSTALLATION).as_dict()}, db)
        store.set_state(LOCK, {"enabled": False, "revision": 0, "attempts": 0, "until": 0}, db)
        store.set_state(ROLE, source_role.as_dict(), db)
        provision_runtime(store, OTHER_INSTALLATION, db)
    manager = DesktopManagement(store, OTHER_INSTALLATION, OTHER_SID)
    desktop_identity.bind(store, installation=OTHER_INSTALLATION, owner=manager.actor, room=source_owner,
                          logical_role=source_role,
                          source={"installation": "installation-test", "kit": "synthetic-kit"})
    return manager, source_owner, (completed, waiting, cancelled)


async def test_restored_installation_reads_and_continues_source_manual_tasks(service, restored_store):
    manager, source_owner, (completed, waiting, cancelled) = await _restored_installation(service, restored_store)
    assert manager.actor.key != source_owner.key and source_owner.scope == "installation-test"
    assert restored_store.state(BINDING)["room"] == source_owner.as_dict()
    manager.bili = Bili()
    runtime = DesktopRuntime(manager, "manual-identity-b", model=Model())
    manual = runtime.manual
    try:
        # The continued identity is the source local user, in its own installation's conversation.
        assert manual.actor == source_owner and manual.conversation == SOURCE_CONVERSATION
        # A restored queue is retired instead of resumed.
        assert restored_store.state(QUEUE) == []
        assert manual.engine._state(waiting)["status"] == "cancelled"
        assert manual.engine._state(waiting)["error"] == "application_restarted"
        assert not await runtime.tasks.run(manual.step)
        assert len(restored_store.records("task_results")[0]) == 1
        # Old tasks stay visible and readable under the continued identity.
        items = {item["id"]: item for item in manual.listing(manager.session_id)["items"]}
        assert set(items) == {completed, waiting, cancelled}
        assert items[completed]["status"] == "completed"
        assert items[completed]["result"]["summary"] == "来源安装已完成的结果"
        assert manual.detail(manager.session_id, completed, 0)["text"] == "来源安装已完成的结果"
        # A new task is written as the source actor into the source conversation.
        manual.runner = synthetic("恢复后新结果")
        fresh = manual.submit(manager.session_id, "video", ARGS, secrets.token_hex(16))["id"]
        record = restored_store.read("task_requests", fresh)["data"]
        assert record["actor"] == source_owner.as_dict() and record["conversation"] == SOURCE_CONVERSATION
        await runtime.tasks.run(manual.step)
        assert manual.engine._state(fresh)["status"] == "completed"
        assert manual.detail(manager.session_id, fresh, 0)["text"] == "恢复后新结果"
        policy = GovernanceRepository(restored_store).policy(
            "task_results/" + manual.engine._state(fresh)["latest"])
        assert policy.owners == {source_owner.key} and policy.conversation == SOURCE_CONVERSATION
        # An old cancelled task continues only on an explicit retry, as the same actor.
        manual.runner = synthetic("重试后续写结果")
        manual.control(manager.session_id, cancelled, "retry", manual.engine._state(cancelled)["revision"])
        await runtime.tasks.run(manual.step)
        assert manual.engine._state(cancelled)["status"] == "completed"
        assert manual.detail(manager.session_id, cancelled, 0)["text"] == "重试后续写结果"
    finally:
        runtime.close()


async def test_management_and_lock_stay_with_the_current_windows_owner(service, restored_store):
    manager, source_owner, (completed, _, _) = await _restored_installation(service, restored_store)
    manager.bili = Bili()
    runtime = DesktopRuntime(manager, "manual-identity-c", model=Model())
    manual = runtime.manual
    try:
        # Management keeps the current physical administrator, never the continued identity.
        management = manager.request()
        assert management.actor == manager.actor.key and management.administrator is True
        room = manager.room.sessions.request(manager.session_id, "role")
        assert room.actor == source_owner.key and room.administrator is False
        with pytest.raises(AssistantError) as denied:
            manager.authority.require(room, administrator=True)
        assert denied.value.code == "access_denied"
        assert call(manager, "session.status")["result"]["windows_user_verified"] is True
        listed = call(manager, "tasks.list", {"session": manager.session_id})
        assert listed["ok"] and completed in {item["id"] for item in listed["result"]["items"]}
        # Locking the current installation stops every continued read and write.
        assert call(manager, "lock.enable", {"password": OTHER_PASSWORD})["ok"]
        assert call(manager, "session.lock")["ok"]
        attempts = [lambda: manual.listing(manager.session_id),
                    lambda: manual.detail(manager.session_id, completed, 0),
                    lambda: manual.submit(manager.session_id, "video", ARGS, secrets.token_hex(16)),
                    lambda: manual.control(manager.session_id, completed, "cancel", "revision")]
        for attempt in attempts:
            with pytest.raises(AssistantError) as denied:
                attempt()
            assert denied.value.code == "desktop_locked"
        assert call(manager, "tasks.list", {"session": manager.session_id})["error"] == "desktop_locked"
        # Unlocking the current installation restores the historical identity's access.
        assert call(manager, "session.unlock", {"password": OTHER_PASSWORD})["ok"]
        assert manual.detail(manager.session_id, completed, 0)["text"] == "来源安装已完成的结果"
    finally:
        runtime.close()


async def test_old_task_retry_rechecks_the_current_session_account_and_model(service, restored_store):
    manager, _, (_, waiting, cancelled) = await _restored_installation(service, restored_store)
    manager.bili = Bili()
    runtime = DesktopRuntime(manager, "manual-identity-d", model=Model())
    manual = runtime.manual
    try:
        manual.runner = synthetic("不应执行")
        with pytest.raises(AssistantError) as denied:
            manual.listing("forged-session")
        assert denied.value.code == "desktop_locked"
        with pytest.raises(AssistantError) as denied:
            manual.control("forged-session", cancelled, "retry", manual.engine._state(cancelled)["revision"])
        assert denied.value.code == "desktop_locked"
        # The B站 account recorded with the source task is still checked before any retry.
        manager.bili.uid = "456"
        with pytest.raises(AssistantError) as denied:
            manual.control(manager.session_id, cancelled, "retry", manual.engine._state(cancelled)["revision"])
        assert denied.value.code == "account_changed"
        # A model that cannot run the task stops the retry as well.
        manager.bili.uid = "123"
        manager.codex = SimpleNamespace(ready_for_model=lambda: False)
        with pytest.raises(AssistantError) as denied:
            manual.control(manager.session_id, cancelled, "retry", manual.engine._state(cancelled)["revision"])
        assert denied.value.code == "model_capability_unavailable"
        manager.codex = None
        # Neither a restart nor a refused retry ever restarts an old cancelled task.
        assert manual.engine._state(cancelled)["status"] == "cancelled"
        assert manual.engine._state(waiting)["status"] == "cancelled"
        assert restored_store.state(QUEUE) == []
        assert not await runtime.tasks.run(manual.step)
        assert len(restored_store.records("task_results")[0]) == 1
        # The same old task still continues once the current checks pass.
        manual.runner = synthetic("重试结果")
        manual.control(manager.session_id, cancelled, "retry", manual.engine._state(cancelled)["revision"])
        await runtime.tasks.run(manual.step)
        assert manual.engine._state(cancelled)["status"] == "completed"
    finally:
        runtime.close()


async def test_third_party_private_manual_tasks_stay_invisible(service, restored_store):
    manager, _, (completed, _, _) = await _restored_installation(service, restored_store)
    manager.bili = Bili()
    runtime = DesktopRuntime(manager, "manual-identity-e", model=Model())
    manual = runtime.manual
    try:
        # Another local user, and the current installation's own manual conversation, are not continued.
        stranger = ActorRef("local", uuid.uuid4().hex, "installation-c")
        foreign = _intruder(restored_store, stranger, "desktop-manual:installation-c")
        physical = _intruder(restored_store, manager.actor, "desktop-manual:" + OTHER_INSTALLATION)
        items = {item["id"] for item in manual.listing(manager.session_id)["items"]}
        assert completed in items and not items & {foreign, physical}
        for identifier in (foreign, physical):
            with pytest.raises(AssistantError) as denied:
                manual.detail(manager.session_id, identifier, 0)
            assert denied.value.code == "access_denied"
            with pytest.raises(AssistantError) as denied:
                manual.control(manager.session_id, identifier, "retry",
                               manual.engine._state(identifier)["revision"])
            assert denied.value.code == "access_denied"
        # The stored disclosure policy still denies them below the ownership filter.
        request = manual.request(manager.session_id)
        for identifier in (foreign, physical):
            with pytest.raises(AssistantError) as denied:
                manual.engine.detail(request, identifier)
            assert denied.value.code == "access_denied"
        assert restored_store.state(QUEUE) == []
        assert not await runtime.tasks.run(manual.step)
    finally:
        runtime.close()


def test_without_a_binding_manual_tasks_stay_with_the_current_owner(service):
    assert service.store.state(BINDING) is None
    service.bili = Bili()
    runtime = DesktopRuntime(service, "manual-identity-unbound", model=Model())
    manual = runtime.manual
    try:
        # Without a binding the unchanged behaviour is the current owner and its installation.
        assert manual.actor == service.actor
        assert manual.conversation == "desktop-manual:" + service.installation
        manual.runner = synthetic("本机结果")
        identifier = manual.submit(service.session_id, "video", ARGS, secrets.token_hex(16))["id"]
        record = service.store.read("task_requests", identifier)["data"]
        assert record["actor"] == service.actor.as_dict() and record["conversation"] == SOURCE_CONVERSATION
        assert manual.listing(service.session_id)["items"][0]["id"] == identifier
    finally:
        runtime.close()
