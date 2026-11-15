from threading import Event

import pytest

from companion_core import postgres_transfer
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from qiban.desktop_qq import KEY, DesktopQQ
from qiban.desktop_qq_targets import adapter
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service

ACCOUNT = {"uin": "10001", "uid": "u_test", "label": "测试🙂", "can_quick_login": True}


class Worker:
    process = None

    def __init__(self, config):
        self.closed = False
        self.calls = []
        self.waiting, self.release = Event(), Event()

    def open(self):
        return {"status": "ready"}

    def request(self, command):
        self.calls.append(command)
        if command == "login_accounts":
            return {"accounts": [ACCOUNT]}
        if command == "login_status":
            self.waiting.set()
            assert self.release.wait(5)
            return {"status": "authenticated", "uin": ACCOUNT["uin"]}
        return {}

    def close(self):
        self.closed = True
        self.release.set()


@pytest.fixture
def manager(service, tmp_path):
    workers = []

    def factory(config):
        worker = Worker(config)
        workers.append(worker)
        return worker

    service.qq = DesktopQQ(service.store, tmp_path / "resources", tmp_path / "account", worker_factory=factory)
    yield service.qq, workers
    service.qq.close()
    assert all(worker.closed for worker in workers)


def test_refresh_requires_explicit_selection_and_never_requests_qr(service, manager):
    qq, workers = manager
    assert call(service, "qq.refresh")["ok"]
    qq.thread.join(5)
    status = call(service, "qq.status")["result"]
    assert not status["busy"] and status["accounts"] == [ACCOUNT] and status["selected"] is None
    assert workers[0].calls == ["login_accounts", "close"]
    assert call(service, "qq.select", {"account": "10001", "expected": 0})["ok"]
    assert qq.status()["selected"]["uin"] == "10001"
    assert service.store.state("core-run:qq") is None
    assert call(service, "qq.select", {"account": "10001", "expected": 0})["error"] == "edit_conflict"


def test_cancel_closes_owned_worker_and_does_not_select_late_login(service, manager):
    qq, workers = manager
    assert call(service, "qq.login")["ok"]
    assert workers[0].waiting.wait(5)
    assert call(service, "qq.cancel")["result"]["cancel_requested"]
    qq.thread.join(5)
    assert workers[0].closed and qq.status()["state"] == "cancelled"
    assert "png_base64" not in qq.status() and qq.status()["selected"] is None
    assert "login_accounts" not in workers[0].calls


def test_lock_blocks_qq_management_and_caller_cannot_supply_identity(service, manager):
    assert call(service, "qq.select", {"account": "10001", "expected": 0, "uid": "u_other"})["error"] == "management_operation"
    assert call(service, "lock.enable", {"password": "synthetic-test-password"})["ok"]
    assert call(service, "session.lock")["ok"]
    for op in ("qq.status", "qq.login", "qq.refresh", "qq.cancel"):
        assert call(service, op)["error"] == "desktop_locked"


def test_password_login_requires_explicit_selection_and_never_exposes_password(service, manager):
    qq, workers = manager
    received = []

    class PasswordWorker(Worker):
        def request(self, command, **args):
            if command == "login_password":
                received.append((command, dict(args)))
                return {"status": "password_pending"}
            if command == "login_status":
                return {"status": "authenticated", "uin": ACCOUNT["uin"]}
            return super().request(command)

    def factory(config):
        worker = PasswordWorker(config)
        workers.append(worker)
        return worker

    qq.worker_factory = factory
    password = "synthetic-qq-password"
    assert call(service, "qq.password", {"account": "10001", "password": password})["ok"]
    qq.thread.join(5)
    assert received == [("login_password", {"account": "10001", "password": password})]
    status = call(service, "qq.status")["result"]
    assert status["state"] == "available" and status["selected"] is None
    assert password not in str(status) and password not in str(service.store.state(KEY))
    assert "login_qr" not in workers[0].calls
    assert service.store.state("core-run:qq") is None


@pytest.mark.parametrize("account,password", [("123", "pw"), ("10001", ""), ("10001", "a"*129), ("10001", "a\0b")])
def test_password_login_rejects_invalid_input_before_worker(service, manager, account, password):
    _, workers = manager
    assert not call(service, "qq.password", {"account": account, "password": password})["ok"]
    assert not workers


def test_selection_is_local_encrypted_state_and_not_restored_from_role_backup(service, manager):
    import secrets

    qq, _ = manager
    qq.begin("refresh")
    qq.thread.join(5)
    qq.select("10001", 0)
    with service.store.db() as db:
        assert db.execute("SELECT value FROM state WHERE key=?", (KEY,)).fetchone()[0].startswith("enc1:")
    key = secrets.token_bytes(32)
    backup = postgres_transfer.create(service.store, key)
    snapshot = postgres_transfer.unpack(backup, key)
    state = snapshot["tables"]["state"]
    assert KEY not in [row[state["columns"].index("key")] for row in state["rows"]]
    qq.select("10001", 1)
    postgres_transfer.restore(service.store, backup, key, expected=postgres_transfer.fingerprint(service.store))
    assert qq.status()["revision"] == 2


class TargetWorker(Worker):
    def __init__(self, config):
        super().__init__(config)
        self.identity = {**config["account"], "friends": {v: "u_" + v for v in config["private_targets"]},
                         "groups": config["groups"]}

    def open(self):
        return self.identity

    def request(self, command):
        self.calls.append(command)
        return self.identity if command == "identity" else {}


def selected_manager(service, manager):
    qq, _ = manager
    qq.begin("refresh")
    qq.thread.join(5)
    qq.select("10001", 0)
    qq.target_factory = TargetWorker
    return qq


def verified(service, qq, peers, groups, revision):
    assert call(service, "qq.targets.verify", {"private_targets": peers, "groups": groups, "expected": revision})["ok"]
    qq.thread.join(5)
    assert not qq.thread.is_alive()
    status = qq.status()
    assert status["state"] == "targets_verified"
    return status["preview"]["token"]


def test_target_verification_never_grants_and_save_creates_formal_adapter(service, manager):
    qq = selected_manager(service, manager)
    token = verified(service, qq, ["20001"], ["30001"], 1)
    scope = ReplyScope("qq", "10001", "private", "20001")
    assert ReplyAuthorizations(service.store).version(scope) is None
    assert adapter(qq) is None
    assert call(service, "qq.targets.save", {"token": token, "expected": 1})["ok"]
    module = adapter(qq)
    assert len(module.reply_targets()) == 2
    assert module.provider is None and service.store.state("core-run:qq") is None
    assert ReplyAuthorizations(service.store).version(scope)
    assert qq.status()["targets"]["authorized"]
    assert call(service, "qq.targets.save", {"token": token, "expected": 1})["error"] == "edit_conflict"


def test_replacing_targets_revokes_removed_scopes_atomically(service, manager):
    qq = selected_manager(service, manager)
    token = verified(service, qq, ["20001"], ["30001"], 1)
    assert call(service, "qq.targets.save", {"token": token, "expected": 1})["ok"]
    token = verified(service, qq, [], [], 2)
    assert call(service, "qq.targets.save", {"token": token, "expected": 2})["ok"]
    auth = ReplyAuthorizations(service.store)
    assert auth.version(ReplyScope("qq", "10001", "private", "20001")) is None
    assert auth.version(ReplyScope("qq", "10001", "group", "30001")) is None
    assert adapter(qq).reply_targets() == ()


def test_changed_permission_rejects_stale_preview_and_rolls_back_removal(service, manager):
    qq = selected_manager(service, manager)
    token = verified(service, qq, ["20001"], ["30001"], 1)
    assert call(service, "qq.targets.save", {"token": token, "expected": 1})["ok"]
    token = verified(service, qq, ["20001"], [], 2)
    auth = ReplyAuthorizations(service.store)
    scope = ReplyScope("qq", "10001", "private", "20001")
    auth.update(scope, enabled=False, expected_version=auth.version(scope), actor="another-management-request")
    assert call(service, "qq.targets.save", {"token": token, "expected": 2})["error"] == "edit_conflict"
    assert auth.version(ReplyScope("qq", "10001", "group", "30001"))
    assert qq.status()["revision"] == 2


def test_failed_or_cancelled_verification_preserves_saved_targets(service, manager):
    qq = selected_manager(service, manager)
    token = verified(service, qq, ["20001"], [], 1)
    assert call(service, "qq.targets.save", {"token": token, "expected": 1})["ok"]

    class Blocked(TargetWorker):
        def open(self):
            self.waiting.set()
            assert self.release.wait(5)
            return self.identity

    worker = Blocked({"account": {"uin": "10001", "uid": "u_test"}, "private_targets": ["20002"], "groups": []})
    qq.target_factory = lambda _: worker
    assert call(service, "qq.targets.verify", {"private_targets": ["20002"], "groups": [], "expected": 2})["ok"]
    assert worker.waiting.wait(5)
    assert call(service, "qq.cancel")["ok"]
    qq.thread.join(5)
    assert qq.status()["preview"] is None
    assert qq.status()["targets"]["private_targets"] == ["20001"]
    assert qq.status()["revision"] == 2


def test_active_run_and_wrong_identity_block_target_changes(service, manager):
    qq = selected_manager(service, manager)
    service.store.set_state("core-run:qq", {"status": "active"})
    args = {"private_targets": ["20001"], "groups": [], "expected": 1}
    assert call(service, "qq.targets.verify", args)["error"] == "qq_stop_required"
    service.store.set_state("core-run:qq", {"status": "paused"})
    worker = TargetWorker({"account": {"uin": "10002", "uid": "u_other"}, "private_targets": ["20001"], "groups": []})
    qq.target_factory = lambda _: worker
    assert call(service, "qq.targets.verify", args)["ok"]
    qq.thread.join(5)
    assert qq.status()["state"] == "error" and qq.status()["preview"] is None
    assert qq.status()["targets"] is None


def test_saved_scopes_survive_resource_relocation_but_not_changed_account_or_targets(service, manager, tmp_path):
    from companion_qq.module import QQModule

    qq = selected_manager(service, manager)
    token = verified(service, qq, ["20001"], ["30001"], 1)
    assert call(service, "qq.targets.save", {"token": token, "expected": 1})["ok"]
    qq.runtime = tmp_path / "updated-resources"
    qq.data = tmp_path / "moved-account"
    module = adapter(qq)
    assert len(module.reply_targets()) == 2
    for changes in ({"uid": "u_another"}, {"groups": ["30002"]}, {"account": "10002"}):
        changed = QQModule(qq.store, tmp_path, options={**module.options, **changes})
        assert changed.reply_targets() == ()


def test_saved_target_changes_attach_to_running_host_without_starting_role(service, manager):
    from qiban.desktop_runtime import DesktopRuntime

    qq = selected_manager(service, manager)
    runtime = DesktopRuntime(service, "qq-dynamic-test")
    runtime.open()
    try:
        token = verified(service, qq, ["20001"], [], 1)
        assert call(service, "qq.targets.save", {"token": token, "expected": 1})["ok"]
        core = runtime.cores["qq"]
        assert core.platforms.all()[0].account == "10001"
        assert core.platforms.all()[0].provider is None
        assert service.store.state("core-run:qq") is None
        assert call(service, "runtime.control", {"platform": "qq", "action": "start"})["error"] == "model_unavailable"
        token = verified(service, qq, ["20002"], [], 2)
        assert call(service, "qq.targets.save", {"token": token, "expected": 2})["ok"]
        assert runtime.cores["qq"] is not core
        assert runtime.cores["qq"].platforms.all()[0].options["private_targets"] == ["20002"]
        assert service.store.state("core-run:qq") is None
    finally:
        runtime.close()


def test_legacy_enrollment_upgrade_requires_exact_saved_configuration(service, manager):
    from companion_qq.module import digest, enrollment_intent

    qq = selected_manager(service, manager)
    token = verified(service, qq, ["20001"], ["30001"], 1)
    assert call(service, "qq.targets.save", {"token": token, "expected": 1})["ok"]
    options = adapter(qq).options
    key = "qq-enrollment:10001"
    enrollment = service.store.state(key)
    service.store.set_state(key, {**enrollment, "intent": digest(options)})
    assert len(adapter(qq).reply_targets()) == 2
    assert service.store.state(key)["intent"] == enrollment_intent(options)
    stale = {**enrollment, "intent": digest({**options, "groups": ["30002"]})}
    service.store.set_state(key, stale)
    assert adapter(qq).reply_targets() == ()
    assert service.store.state(key) == stale


def test_worker_creation_failure_returns_error_and_can_be_explicitly_retried(service, manager):
    qq, _ = manager
    def fail(_):
        raise FileNotFoundError("synthetic missing component")
    qq.worker_factory = fail
    assert call(service, "qq.refresh")["ok"]
    qq.thread.join(5)
    assert qq.status()["state"] == "error"
    assert "组件" in qq.status()["message"]
    assert not qq.status()["busy"] and qq.worker is None
    qq.worker_factory = Worker
    assert call(service, "qq.refresh")["ok"]
    qq.thread.join(5)
    assert qq.status()["state"] == "available"


class DirectoryWorker(TargetWorker):
    def request(self, command):
        self.calls.append(command)
        if command == "directory":
            return {"private_targets": [{"id": "20001", "label": "备注🙂"}],
                    "groups": [{"id": "30001", "label": "测试群"}]}
        return self.identity if command == "identity" else {}


def test_directory_is_read_only_and_selecting_account_clears_names(service, manager):
    qq = selected_manager(service, manager)
    qq.target_factory = DirectoryWorker
    saved = service.store.state(KEY)
    assert call(service, "qq.directory", {"expected": 1})["ok"]
    qq.thread.join(5)
    status = qq.status()
    assert status["state"] == "directory_ready"
    assert status["directory"]["groups"][0]["label"] == "测试群"
    assert service.store.state(KEY) == saved and adapter(qq) is None
    assert ReplyAuthorizations(service.store).records() == []
    assert service.store.state("core-run:qq") is None
    qq.begin("refresh")
    qq.thread.join(5)
    assert qq.status()["directory"] is None


def test_directory_cancel_and_wrong_identity_cannot_publish_names(service, manager):
    qq = selected_manager(service, manager)
    class Blocked(DirectoryWorker):
        def request(self, command):
            if command == "directory":
                self.waiting.set()
                assert self.release.wait(5)
            return super().request(command)
    config = {"account": {"uin": "10001", "uid": "u_test"}, "private_targets": [], "groups": []}
    worker = Blocked(config)
    qq.target_factory = lambda _: worker
    assert call(service, "qq.directory", {"expected": 1})["ok"]
    assert worker.waiting.wait(5)
    assert call(service, "qq.cancel")["ok"]
    qq.thread.join(5)
    assert qq.status()["state"] == "cancelled" and qq.status()["directory"] is None
    worker = DirectoryWorker({**config, "account": {"uin": "10002", "uid": "u_other"}})
    assert call(service, "qq.directory", {"expected": 1})["ok"]
    qq.thread.join(5)
    assert qq.status()["state"] == "error" and qq.status()["directory"] is None


def test_cleanup_failure_is_visible_and_blocks_reuse(service, manager):
    from qiban.desktop_runtime import DesktopRuntime

    qq = selected_manager(service, manager)
    class BrokenCleanup(DirectoryWorker):
        def close(self):
            self.closed = True
            raise RuntimeError("synthetic cleanup failure")
    qq.target_factory = BrokenCleanup
    assert call(service, "qq.directory", {"expected": 1})["ok"]
    qq.thread.join(5)
    assert qq.status()["error"] == "qq_cleanup_failed" and qq.status()["directory"] is None
    assert call(service, "qq.refresh")["error"] == "qq_cleanup_failed"
    runtime = DesktopRuntime(service, "directory-cleanup")
    try:
        assert call(service, "runtime.control", {"platform": "qq", "action": "start"})["error"] == "qq_cleanup_failed"
    finally:
        runtime.close()
