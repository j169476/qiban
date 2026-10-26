from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, ReplyTarget
from companion_service.reply_import import PREFIX
from companion_service.reply_management import ReplyManagement
from tests_postgres.test_web import service as service


@pytest.fixture
def imports(service, monkeypatch):
    targets = [ReplyTarget(ReplyScope("qq", "10001", "private", str(10002 + i)), "测试", "测试机器人")
               for i in range(3)]
    monkeypatch.setattr(ReplyManagement, "targets", lambda self: ({t.scope.id: t for t in targets}, {}))
    service.control("synthetic-bridge-secret", "admin", "management_open", {})

    def call(name, args):
        return service.control("synthetic-bridge-secret", "admin", "reply_import_" + name, args)

    return call, targets


def test_import_preview_dedup_and_confirm_append_without_touching_existing(service, imports):
    call, targets = imports
    permissions = ReplyAuthorizations(service.store)
    old = permissions.update(targets[2].scope, enabled=True, expected_version=None, actor="test")
    preview = call("preview", {"platform": "qq", "identifiers": ["10002", "10002", "10003", "10004"]})
    assert preview["confirmable"] and preview["duplicates_removed"] == 1
    assert len(permissions.records()) == 1
    result = call("confirm", {"token": preview["token"]})
    assert result["added"] == 2 and result["already_authorized"] == 1
    assert permissions.record(targets[2].scope)["version"] == old["version"]
    with pytest.raises(AssistantError):
        call("confirm", {"token": preview["token"]})


@pytest.mark.parametrize("fault", ["expired", "actor", "changed", "missing", "unverified"])
def test_import_never_partially_grants_on_invalid_preview(service, imports, fault):
    call, targets = imports
    preview = call("preview", {"platform": "qq", "identifiers": ["10002", "99999" if fault == "unverified" else "10003"]})
    key = PREFIX + preview["token"]
    value = service.store.state(key)
    if fault == "expired":
        value["expires"] = 0
    elif fault == "actor":
        value["actor"] = "another-admin"
    elif fault == "changed":
        ReplyAuthorizations(service.store).update(targets[1].scope, enabled=False, expected_version=None, actor="test")
    elif fault == "missing":
        targets.pop()
        targets.pop()
    service.store.set_state(key, value)
    with pytest.raises(AssistantError):
        call("confirm", {"token": preview["token"]})
    assert ReplyAuthorizations(service.store).version(targets[0].scope) is None


def test_import_pauses_only_changed_platform(service, imports):
    call, _ = imports
    stopped = []
    service.platform_runs.cores["qq"] = SimpleNamespace(run_key="core-run:qq", stop=lambda why: stopped.append(why))
    service.store.set_state("core-run:qq", {"status": "active"})
    preview = call("preview", {"platform": "qq", "identifiers": ["10002"]})
    assert not stopped
    assert call("confirm", {"token": preview["token"]})["platform_paused"]
    assert stopped == ["reply_authorization_changed"]
