"""Authorization boundaries against isolated encrypted PostgreSQL and fake endpoints."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import AUDIT_PREFIX, PREFIX, ReplyAuthorizations, ReplyScope
from companion_service.reply_management import ReplyManagement
from companion_service.service import Service
from companion_wechat.module import WeChatModule
from tests_postgres.test_runtime import Model
from tests_postgres.test_web import ADMIN, PASSWORD, Host, client, login
from tests_postgres.test_wechat_module import compose
from tests_postgres.test_wechat_role import enqueue
from tests_wechat.test_receiver import row


@pytest.fixture
def service(pg_store):
    value = Service(pg_store, Model(), Host(), bridge_secret="synthetic-bridge-secret")
    value.sessions.configure(value.sessions.setup_ticket(), PASSWORD)
    return value


def scope_of(env, alias="owner_main"):
    binding = env.receiver.bindings.get(alias)
    return ReplyScope("wechat", env.receiver.bindings.identity, binding.kind, binding.target_ref)


def change(store, scope, enabled):
    repo = ReplyAuthorizations(store)
    return repo.update(scope, enabled=enabled, expected_version=repo.record(scope)["version"], actor="synthetic-admin")


def test_default_deny_scopes_cas_and_encryption(pg_store):
    repo = ReplyAuthorizations(pg_store)
    scope = ReplyScope("qq", "10001", "private", "10002")
    assert repo.record(scope)["enabled"] is False and repo.version(scope) is None
    granted = change(pg_store, scope, True)
    assert repo.version(scope) == granted["version"]
    for different in (replace(scope, platform="wechat"), replace(scope, account_identity="10003"),
                      replace(scope, kind="group"), replace(scope, target_id="10004")):
        assert repo.version(different) is None
    with pytest.raises(AssistantError) as conflict:
        repo.update(scope, enabled=False, expected_version=None, actor="synthetic-admin")
    assert conflict.value.code == "edit_conflict"
    assert change(pg_store, scope, True) == granted  # Idempotent, does not invalidate an admitted reply.
    change(pg_store, scope, False)
    regranted = change(pg_store, scope, True)
    assert regranted["version"] != granted["version"]
    with pytest.raises(AssistantError) as stale:
        repo.check(scope, granted["version"])
    assert stale.value.code == "reply_not_authorized"
    with pg_store.db() as db:
        rows = db.execute("SELECT value FROM state WHERE key LIKE ? OR key LIKE ?", (PREFIX + "%", AUDIT_PREFIX + "%"))
        assert all(r[0].startswith("enc1:") and "10002" not in r[0] for r in rows)


def test_restore_revokes_reply_grants(pg_store):
    from companion_core.postgres_transfer import create, fingerprint, restore

    scope = ReplyScope("qq", "10001", "group", "10002")
    original = change(pg_store, scope, True)
    key = b"s" * 32
    blob = create(pg_store, key)
    restore(pg_store, blob, key, expected=fingerprint(pg_store))
    restored = ReplyAuthorizations(pg_store).record(scope)
    assert restored["enabled"] is False and restored["version"] != original["version"]


@pytest.mark.asyncio
async def test_unknown_chats_never_enter_role_and_authorization_does_not_replay_history(pg_store):
    env = await compose(pg_store, family=True, authorized=False)
    try:
        env.core.start()
        enqueue(env, row(1), row(1, group=True))
        assert await env.adapter.collect_once() == []
        assert env.model.calls == env.driver.sent == []
        change(pg_store, scope_of(env), True)
        enqueue(env, row(1), row(2), row(2, group=True))
        incoming = await env.adapter.collect_once()
        assert len(incoming) == 1 and incoming[0].conversation == "owner_main"
        assert (await env.core.handle(incoming[0]))["action"]["status"] == "succeeded"
        assert len(env.driver.sent) == 1
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_revoke_and_regrant_do_not_resurrect_old_admissions_across_restart(pg_store):
    env = await compose(pg_store)
    env.core.start()
    enqueue(env, row(1))
    admitted = await env.adapter.collect_once()
    change(pg_store, scope_of(env), False)
    change(pg_store, scope_of(env), True)
    assert env.session.admitted_pending() == []
    with pytest.raises(AssistantError) as stale:
        await env.core.handle(admitted[0])
    assert stale.value.code == "reply_not_authorized"
    checkpoint = env.receiver._saved
    env.core.stop()
    await env.adapter.close()
    resumed = await compose(pg_store, previous=checkpoint, authorized=False)
    try:
        resumed.core.start()
        enqueue(resumed, row(2))
        incoming = await resumed.adapter.collect_once()
        assert len(incoming) == 1 and incoming[0].id != admitted[0].id
        assert (await resumed.core.handle(incoming[0]))["action"]["status"] == "succeeded"
        assert not env.driver.sent and len(resumed.driver.sent) == 1
    finally:
        resumed.core.stop()
        await resumed.adapter.close()


@pytest.mark.asyncio
async def test_revoke_during_prepare_prevents_native_submit(pg_store):
    env = await compose(pg_store)
    try:
        env.core.start()
        enqueue(env, row(1))
        incoming = await env.adapter.collect_once()
        env.driver.before = lambda: change(pg_store, scope_of(env), False)
        result = await env.core.handle(incoming[0])
        assert result["action"]["status"] == "cancelled"
        assert env.driver.sent == [] and len(env.model.calls) > 0
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_revoke_at_last_native_inspection_prevents_submit(pg_store):
    env = await compose(pg_store)
    original = env.driver.inspect
    try:
        env.core.start()
        enqueue(env, row(1))
        incoming = await env.adapter.collect_once()

        def inspect(request):
            proof = original(request)
            # Durable claim exists only at the final inspection, after model and
            # preparation. Revoke here to exercise the last submission barrier.
            if pg_store.state(env.sender._action_key(request)) is not None:
                change(pg_store, scope_of(env), False)
            return proof

        env.driver.inspect = inspect
        result = await env.core.handle(incoming[0])
        assert result["action"]["status"] == "cancelled" and not env.driver.sent
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_web_and_trusted_chat_share_authorization_and_pause_only_wechat(service, tmp_path):
    env = await compose(service.store, authorized=False)
    module = WeChatModule(service.store, tmp_path, "synthetic")
    module.account = env.receiver.account
    env.core.platforms = SimpleNamespace(all=lambda: [module])
    service.platform_runs.cores["wechat"] = env.core
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    def operation(name, args=None, h=headers):
        return admin.post("/operate", json={"operation": name, "arguments": args or {}}, headers=h)
    try:
        assert operation("reply_authorization_list", h={"origin": ADMIN}).status_code == 403
        role = client(service, "role")
        role_headers = login(role, service, "role")
        assert role.post("/operate", json={"operation": "reply_authorization_list"}, headers=role_headers).status_code == 404
        assert "会话回复授权" in admin.get("/").text
        listing = operation("reply_authorization_list").json()
        assert len(listing["items"]) == 2 and listing["default_allow"] is False
        assert listing["platforms"] == {"qq": {"available": False}, "wechat": {"available": True}}
        assert all(not r["enabled"] and r["verified_target"] for r in listing["items"])
        target = next(r for r in listing["items"] if r["scope"]["kind"] == "private")
        args = {"scope_id": target["id"], "enabled": True, "expected_version": None}
        assert operation("reply_authorization_set", args | {"scope_id": "unverified"}).status_code == 403
        service.control("synthetic-bridge-secret", "admin", "management_open", {})
        with pytest.raises(AssistantError) as denied:
            service.control("synthetic-bridge-secret", "control", "reply_authorization_set", args)
        assert denied.value.code == "admin_required"
        result = operation("reply_authorization_set", args)
        assert result.status_code == 200, result.text
        granted = result.json()["authorization"]
        from_chat = service.control("synthetic-bridge-secret", "admin", "reply_authorization_list", {})
        assert next(r for r in from_chat["items"] if r["id"] == target["id"])["version"] == granted["version"]
        assert operation("reply_authorization_set", args).status_code == 409
        # Synthetic lifecycle spy: changing one platform never touches another.
        stopped = []
        env.core.stop = lambda reason: stopped.append(reason)
        service.store.set_state(env.core.run_key, {"status": "active"})
        service.store.set_state("core-run:bilibili", {"status": "active", "sentinel": "unchanged"})
        result = service.control("synthetic-bridge-secret", "admin", "reply_authorization_set",
                                 args | {"enabled": False, "expected_version": granted["version"]})
        assert result["platform_paused"] and not result["role_started"]
        assert stopped == ["reply_authorization_changed"]
        assert service.store.state("core-run:bilibili")["sentinel"] == "unchanged"
        assert next(r for r in operation("reply_authorization_list").json()["items"] if r["id"] == target["id"])["enabled"] is False
    finally:
        await env.adapter.close()


@pytest.mark.asyncio
async def test_missing_connector_can_only_revoke_existing_grant(service, tmp_path):
    scope = ReplyScope("qq", "10001", "private", "10002")
    saved = change(service.store, scope, True)
    service.control("synthetic-bridge-secret", "admin", "management_open", {})
    listing = service.control("synthetic-bridge-secret", "admin", "reply_authorization_list", {})
    assert listing["items"][0]["verified_target"] is False
    args = {"scope_id": scope.id, "expected_version": saved["version"], "enabled": True}
    with pytest.raises(AssistantError) as rejected:
        service.control("synthetic-bridge-secret", "admin", "reply_authorization_set", args)
    assert rejected.value.code == "reply_target_unverified"
    service.control("synthetic-bridge-secret", "admin", "reply_authorization_set", args | {"enabled": False})
    assert ReplyAuthorizations(service.store).version(scope) is None


def test_broken_connector_does_not_prevent_revocation_or_other_discovery(service):
    from companion_core.reply_authorization import ReplyTarget

    scope = ReplyScope("wechat", "wechat-db:synthetic", "private", "synthetic-peer")
    saved = change(service.store, scope, True)

    def broken():
        raise RuntimeError("synthetic-source-path-must-not-be-returned")

    other = ReplyTarget(ReplyScope("qq", "10001", "private", "10002"), "contract-only", "synthetic")
    service.platform_runs.cores = {
        "wechat": SimpleNamespace(platforms=SimpleNamespace(all=lambda: [SimpleNamespace(reply_targets=broken)])),
        "qq": SimpleNamespace(platforms=SimpleNamespace(all=lambda: [SimpleNamespace(reply_targets=lambda: (other,))])),
    }
    targets, platforms = ReplyManagement(service).targets()
    assert set(targets) == {other.scope.id} and platforms["qq"]["available"]
    assert platforms["wechat"] == {"available": False, "discovery_failed": True}
    service.control("synthetic-bridge-secret", "admin", "management_open", {})
    # It is an unavailable connector, so there is no runtime to pause.
    service.platform_runs.cores["wechat"].run_key = "core-run:wechat"
    service.control("synthetic-bridge-secret", "admin", "reply_authorization_set", {
        "scope_id": scope.id, "enabled": False, "expected_version": saved["version"]})
    assert ReplyAuthorizations(service.store).version(scope) is None


def test_private_and_group_settings_share_web_and_admin_chat_with_cas_and_platform_isolation(service):
    from companion_core.reply_authorization import ReplyTarget

    stopped = []
    for platform, account in (("qq", "10001"), ("wechat", "wechat-db:synthetic")):
        target = ReplyTarget(ReplyScope(platform, account, "private", "10002"), "synthetic", account)
        adapter = SimpleNamespace(reply_targets=lambda t=target: (t,))
        service.platform_runs.cores[platform] = SimpleNamespace(
            platforms=SimpleNamespace(all=lambda a=adapter: [a]), run_key="core-run:" + platform,
            stop=lambda reason, p=platform: stopped.append((p, reason)))
        service.store.set_state("core-run:" + platform, {"status": "active"})
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    def operation(name, args=None, h=headers):
        return admin.post("/operate", json={"operation": name, "arguments": args or {}}, headers=h)
    assert operation("reply_policy_list", h={"origin": ADMIN}).status_code == 403
    role = client(service, "role")
    role_headers = login(role, service, "role")
    assert role.post("/operate", json={"operation": "reply_policy_list"}, headers=role_headers).status_code == 404
    service.control("synthetic-bridge-secret", "admin", "management_open", {})
    with pytest.raises(AssistantError):
        service.control("synthetic-bridge-secret", "control", "reply_policy_list", {})
    listing = operation("reply_policy_list").json()
    assert len(listing["items"]) == 2
    assert all(v["settings"]["private_daily_replies"] == 200 for v in listing["items"])
    args = {"platform": "qq", "account_identity": "10001", "changes": {
        "private_daily_replies": 12, "group_session_replies": 3}, "expected_version": None}
    result = operation("reply_policy_set", args)
    assert result.status_code == 200 and not result.json()["platform_paused"]
    assert stopped == []
    assert operation("reply_policy_set", args).status_code == 409
    chat = service.control("synthetic-bridge-secret", "admin", "reply_policy_list", {})
    qq = next(item for item in chat["items"] if item["platform"] == "qq")
    wechat = next(item for item in chat["items"] if item["platform"] == "wechat")
    assert qq["settings"]["private_daily_replies"] == 12
    assert wechat["settings"]["private_daily_replies"] == 200
    args.update(expected_version=qq["version"], changes={"private_daily_replies": 0})
    saved = service.control("synthetic-bridge-secret", "admin", "reply_policy_set", args)
    assert not saved["role_started"]
    assert next(item for item in operation("reply_policy_list").json()["items"]
                if item["platform"] == "qq")["settings"]["private_daily_replies"] == 0
