import pytest
import yaml

from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError
from bili_assistant.relationship_store import pending, receipt, resolve, status
from bili_assistant.storage import Store


@pytest.mark.parametrize("phase", ["receipt", "before_yaml", "after_yaml"])
def test_relationship_commit_recovers_at_each_boundary_and_survives_rotation(role, monkeypatch, phase):
    store = role.store
    write, set_state = store.write, store.set_state

    def failed_write(*args, **kwargs):
        if phase == "after_yaml":
            write(*args, **kwargs)
        raise OSError("injected file interruption")

    def failed_receipt(key, value, db=None):
        if key.startswith("relationship:"):
            raise OSError("injected receipt interruption")
        return set_state(key, value, db)

    with monkeypatch.context() as patch:
        patch.setattr(store, "set_state" if phase == "receipt" else "write",
                      failed_receipt if phase == "receipt" else failed_write)
        with pytest.raises(OSError):
            role.relationship("789", "owner", 1, "original")
    if phase == "receipt":
        assert not store.path("relationships", "789").exists()
        assert not pending(store) and not store.state(receipt("789", "original"))
    reopened = Store(store.root)
    recovered = Engine(reopened, role.host, role.adapter, role.clock)
    result = recovered.relationship("789", "owner", 1, "original")
    assert bool(result.get("duplicate")) == (phase != "receipt")
    assert result["data"]["affinity"] == 51
    assert not pending(reopened)
    for i in range(101):
        recovered.relationship("789", "owner", 0, f"later-{i}")
    assert recovered.relationship("789", "owner", 1, "original")["duplicate"]
    assert reopened.read("relationships", "789")["data"]["interactions"] == 102


@pytest.mark.parametrize("choice,score", [("keep_local", 70), ("apply_pending", 51)])
def test_manual_edit_during_recovery_is_preserved_until_explicit_resolution(role, monkeypatch, choice, score):
    role.relationship("789", "owner", 0, "seed")
    with monkeypatch.context() as patch:
        patch.setattr(role.store, "write", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("stop")))
        with pytest.raises(OSError):
            role.relationship("789", "owner", 1, "pending")
    old = role.store.read("relationships", "789")["data"]
    old["affinity"] = 70
    role.store.path("relationships", "789").write_text(yaml.safe_dump(old), encoding="utf-8")
    reopened = Store(role.store.root)
    assert reopened.read("relationships", "789")["data"]["affinity"] == 70
    item = status(reopened)[0]
    assert item["status"] == "conflict"
    assert not reopened.validate_all()["ok"]
    with pytest.raises(AssistantError, match="变化"):
        resolve(reopened, "789", choice, "stale")
    resolve(reopened, "789", choice, item["current_version"])
    assert reopened.read("relationships", "789")["data"]["affinity"] == score
    assert reopened.state(receipt("789", "pending")) is True
    assert not pending(reopened)


def test_legacy_recent_event_repairs_missing_receipt(role):
    role.relationship("789", "owner", 1, "old")
    with role.store.db() as db:
        db.execute("DELETE FROM state WHERE key=?", (receipt("789", "old"),))
    assert role.relationship("789", "owner", 1, "old")["duplicate"]
    assert role.store.state(receipt("789", "old")) is True


def test_locked_relationship_does_not_consume_new_event(role):
    saved = role.relationship("789", "owner", 0, "seed")
    saved["data"]["locked"] = True
    role.store.write("relationships", saved["data"], saved["version"], "user", "789")
    with pytest.raises(AssistantError, match="锁定"):
        role.relationship("789", "owner", 1, "new")
    assert not role.store.state(receipt("789", "new"))
    assert not pending(role.store)
