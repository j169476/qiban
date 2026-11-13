"""Cross-installation restore identity: current Windows owner keeps management, room keeps history."""
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from keyring.backends.Windows import WinVaultKeyring

from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore
from qiban import desktop_identity
from qiban.desktop_identity import BINDING, ROLE
from qiban.desktop_management import LOCK, OWNER, DesktopManagement
from qiban.desktop_runtime import DesktopRuntime, provision_runtime
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_wechat_role import Model

__all__ = ["desktop_store", "service"]

OTHER_INSTALLATION = "installation-b"
OTHER_SID = "S-1-5-21-test-owner-b"
OTHER_PASSWORD = "another-strong-pass"


def _connection(template):
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
    options = _connection(service)
    schema = "desktop_identity_" + uuid.uuid4().hex
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


def _source_identities(store):
    return ActorRef(**store.state(OWNER)["actor"]), ActorRef(**store.state(ROLE))


async def _restored_installation(service, store):
    """A writes one historical message; its rows are restored, then B stages the adopted role."""
    from companion_core import postgres_transfer

    runtime = DesktopRuntime(service, "identity-app-a", room_model=Model(), adapters=[service.room])
    try:
        token = call(service, "lock.enable", {"password": "temporary-strong-pass"})["result"]["room_session"]
        runtime.start("local")
        posted = call(service, "room.post", {"session": token, "text": "旧会客室消息", "nonce": uuid.uuid4().hex})
        assert posted["ok"] and posted["result"]["new"]
        # A queued input has no conversation history yet; process it through the
        # synthetic model before taking the source snapshot.
        event, = await service.room.runtime_poll()
        await service.room.core.handle(event)
        logical_owner, logical_role = _source_identities(service.store)
        blob = postgres_transfer.create(service.store, service.store._backup_key)
    finally:
        runtime.close()
        service.runtime = None
    restored = postgres_transfer.restore(store, blob, service.store._backup_key,
                                         expected=postgres_transfer.fingerprint(store))
    assert restored["restored"]
    # The physical cutover, then the trusted staging step: the restored role becomes this role.
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        other_owner = ActorRef("local", uuid.uuid4().hex, OTHER_INSTALLATION)
        store.set_state(OWNER, {"format": 1, "installation": OTHER_INSTALLATION, "windows_sid": OTHER_SID,
                                "actor": other_owner.as_dict()}, db)
        store.set_state(LOCK, {"enabled": False, "revision": 0, "attempts": 0, "until": 0}, db)
        store.set_state(ROLE, logical_role.as_dict(), db)
        provision_runtime(store, OTHER_INSTALLATION, db)
    manager = DesktopManagement(store, OTHER_INSTALLATION, OTHER_SID)
    return manager, logical_owner, logical_role


async def _bound_installation(service, store):
    manager, logical_owner, logical_role = await _restored_installation(service, store)
    desktop_identity.bind(store, installation=OTHER_INSTALLATION, owner=manager.actor,
                          room=logical_owner, logical_role=logical_role,
                          source={"installation": "installation-test", "kit": "synthetic-kit"})
    return manager, logical_owner, logical_role


async def test_unbound_adopted_role_is_refused_and_binding_restores_role_and_history(
        service, restored_store):
    manager, logical_owner, logical_role = await _restored_installation(service, restored_store)
    # Without a binding the restored role is not this installation's own scope.
    with pytest.raises(AssistantError) as refused:
        DesktopRuntime(manager, "identity-app-b", room_model=Model(), adapters=[manager.room])
    assert refused.value.code == "runtime_identity"
    desktop_identity.bind(restored_store, installation=OTHER_INSTALLATION, owner=manager.actor,
                          room=logical_owner, logical_role=logical_role)
    token = call(manager, "lock.enable", {"password": OTHER_PASSWORD})["result"]["room_session"]
    runtime = DesktopRuntime(manager, "identity-app-b", room_model=Model(), adapters=[manager.room])
    try:
        assert runtime.role.key == logical_role.key
        runtime.start("local")
        history = call(manager, "room.history", {"session": token, "before": ""})["result"]["messages"]
        assert "旧会客室消息" in [item["text"] for item in history]
    finally:
        runtime.close()


async def test_bound_room_writes_as_the_historical_actor_and_keeps_management(service, restored_store):
    manager, logical_owner, logical_role = await _bound_installation(service, restored_store)
    assert manager.actor.key != logical_owner.key
    assert manager.windows_sid == OTHER_SID
    assert restored_store.state(OWNER)["windows_sid"] == OTHER_SID
    assert call(manager, "session.status")["result"]["windows_user_verified"] is True
    token = call(manager, "lock.enable", {"password": OTHER_PASSWORD})["result"]["room_session"]
    room_request = manager.room.sessions.request(token, "role")
    assert room_request.actor == logical_owner.key and room_request.administrator is False
    # The historical identity never becomes an administrator request.
    with pytest.raises(AssistantError) as denied:
        manager.authority.require(room_request, administrator=True)
    assert denied.value.code == "access_denied"
    assert manager.request().actor == manager.actor.key and manager.request().administrator is True
    runtime = DesktopRuntime(manager, "identity-app-b", room_model=Model(), adapters=[manager.room])
    try:
        assert runtime.role.key == logical_role.key
        runtime.start("local")
        posted = call(manager, "room.post", {"session": token, "text": "恢复后的新消息", "nonce": uuid.uuid4().hex})
        assert posted["ok"] and posted["result"]["new"]
        with restored_store.db() as db:
            actor, account = db.execute("SELECT actor,account FROM core_events WHERE id=?",
                                        (posted["result"]["id"],)).fetchone()
        assert actor == logical_owner.key
        assert account == restored_store.read("manifest")["data"]["project_id"]
        # The binding never carries the current administrator's session or lock.
        assert call(manager, "session.lock")["ok"]
        locked = call(manager, "room.post", {"session": token, "text": "锁定后", "nonce": uuid.uuid4().hex})
        assert locked["ok"] is False and locked["error"] == "desktop_locked"
    finally:
        runtime.close()


@pytest.mark.parametrize("field", ["installation", "owner", "project_id", "logical_role"])
async def test_forged_binding_is_refused(service, restored_store, field):
    manager, _, _ = await _bound_installation(service, restored_store)
    record = dict(restored_store.state(BINDING))
    if field == "installation":
        record["installation"] = "installation-other"
    elif field == "owner":
        record["owner"] = ActorRef("local", uuid.uuid4().hex, OTHER_INSTALLATION).as_dict()
    elif field == "project_id":
        record["project_id"] = "other-project"
    else:
        record["logical_role"] = {**record["logical_role"], "user_id": uuid.uuid4().hex}
    restored_store.set_state(BINDING, record)
    with pytest.raises(AssistantError) as refused:
        manager.room_actor()
    assert refused.value.code == "desktop_identity_invalid"
    with pytest.raises(AssistantError) as refused:
        DesktopRuntime(manager, "forged-app", room_model=Model(), adapters=[manager.room])
    assert refused.value.code == "desktop_identity_invalid"


@pytest.mark.parametrize("corrupt", ["text", "list", "empty"])
async def test_corrupt_binding_is_refused_not_treated_as_unbound(service, restored_store, corrupt):
    manager, _, _ = await _bound_installation(service, restored_store)
    restored_store.set_state(BINDING, {"text": "corrupt", "list": ["desktop"], "empty": {}}[corrupt])
    with pytest.raises(AssistantError) as refused:
        manager.room_actor()
    assert refused.value.code == "desktop_identity_invalid"
    with pytest.raises(AssistantError) as refused:
        DesktopRuntime(manager, "corrupt-app", room_model=Model(), adapters=[manager.room])
    assert refused.value.code == "desktop_identity_invalid"


async def test_binding_rejects_foreign_or_unstaged_identities(service, restored_store):
    manager, logical_owner, logical_role = await _restored_installation(service, restored_store)
    cases = [
        ({"platform": "qq", "user_id": logical_owner.user_id, "scope": logical_owner.scope}, logical_role),
        (logical_owner, ActorRef("local", uuid.uuid4().hex, "installation-test")),
        ({"platform": "local", "user_id": "x" * 201, "scope": logical_owner.scope}, logical_role),
        (ActorRef("local", uuid.uuid4().hex, "installation test"), logical_role),
        (logical_owner, ActorRef("qiban-role", uuid.uuid4().hex, OTHER_INSTALLATION)),
    ]
    for room, role in cases:
        with pytest.raises(AssistantError) as refused:
            desktop_identity.bind(restored_store, installation=OTHER_INSTALLATION, owner=manager.actor,
                                  room=room, logical_role=role)
        assert refused.value.code == "desktop_identity_invalid"
    with pytest.raises(AssistantError) as refused:
        desktop_identity.bind(restored_store, installation=OTHER_INSTALLATION,
                              owner=ActorRef("local", uuid.uuid4().hex, OTHER_INSTALLATION),
                              room=logical_owner, logical_role=logical_role)
    assert refused.value.code == "desktop_identity_invalid"
    assert restored_store.state(BINDING) is None


async def test_repeated_binding_keeps_the_historical_identity(service, restored_store):
    manager, logical_owner, logical_role = await _restored_installation(service, restored_store)
    first = desktop_identity.bind(restored_store, installation=OTHER_INSTALLATION, owner=manager.actor,
                                  room=logical_owner, logical_role=logical_role)
    again = desktop_identity.bind(restored_store, installation=OTHER_INSTALLATION, owner=manager.actor,
                                  room=logical_owner, logical_role=logical_role)
    assert again["room"] == first["room"] and again["logical_role"] == first["logical_role"]
    with pytest.raises(AssistantError) as refused:
        desktop_identity.bind(restored_store, installation=OTHER_INSTALLATION, owner=manager.actor,
                              room=ActorRef("local", uuid.uuid4().hex, "installation-c"),
                              logical_role=logical_role)
    assert refused.value.code == "desktop_identity_conflict"


async def test_without_binding_the_room_uses_the_current_owner(service):
    assert service.store.state(BINDING) is None
    assert service.room_actor() == service.actor
    assert service.room.owner_actor() == service.actor
    token = call(service, "lock.enable", {"password": "temporary-strong-pass"})["result"]["room_session"]
    request = service.room.sessions.request(token, "role")
    assert request.actor == service.actor.key and request.administrator is False
    role = service.store.state(ROLE)
    service.store.set_state(ROLE, {**role, "scope": "other-installation"})
    with pytest.raises(AssistantError) as refused:
        DesktopRuntime(service, "unbound-app", room_model=Model(), adapters=[service.room])
    assert refused.value.code == "runtime_identity"


def _backup_carrying(service, key, value, other_key, other_value):
    """A snapshot taken elsewhere can carry another installation's role and logical identity."""
    current = {key: service.store.state(key), other_key: service.store.state(other_key)}
    service.store.set_state(key, value)
    service.store.set_state(other_key, other_value)
    try:
        return Path(service.store.backup(force=True)["path"]).name
    finally:
        if current[key] is None:
            with service.store.db() as db:
                db.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            service.store.set_state(key, current[key])
        service.store.set_state(other_key, current[other_key])


def _restore_local(service, name):
    preview = service.backups.prepare(name)
    return service.backups.complete(service.backups.begin(preview["token"])["challenge"])


def test_local_restore_keeps_this_installation_identity(service):
    assert service.store.state(BINDING) is None
    foreign_owner = ActorRef("local", uuid.uuid4().hex, "installation-other")
    foreign_role = ActorRef("qiban-role", uuid.uuid4().hex, "installation-other")
    foreign = {"format": 1, "installation": "installation-other", "owner": foreign_owner.as_dict(),
               "project_id": "other-project", "logical_role": foreign_role.as_dict(),
               "room": foreign_owner.as_dict(), "source": None, "bound_at": "2026-11-07T00:00:00Z"}
    # Without a binding, a snapshot carrying another installation's role is not adopted.
    role_before = service.store.state(ROLE)
    name = _backup_carrying(service, BINDING, foreign, ROLE, foreign_role.as_dict())
    assert _restore_local(service, name)["restored"]
    assert service.store.state(BINDING) is None and service.store.state(ROLE) == role_before
    # Staged adoption: the restored role becomes this installation's role, then the binding.
    adopted = ActorRef("qiban-role", uuid.uuid4().hex, service.installation)
    service.store.set_state(ROLE, adopted.as_dict())
    bound = desktop_identity.bind(service.store, installation=service.installation, owner=service.actor,
                                  room=ActorRef("local", uuid.uuid4().hex, service.installation),
                                  logical_role=adopted)
    name = _backup_carrying(service, BINDING, foreign, ROLE, foreign_role.as_dict())
    assert _restore_local(service, name)["restored"]
    assert service.store.state(BINDING) == bound
    assert service.store.state(ROLE) == adopted.as_dict()
    reopened = DesktopRuntime(service, "reopen-app", room_model=Model(), adapters=[service.room])
    try:
        assert reopened.role.key == adopted.key
    finally:
        reopened.close()
