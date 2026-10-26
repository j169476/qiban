"""Auth boundaries and failure recovery, using isolated data and an in-memory keyring."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bilibili_api import Credential, user
from conftest import FakeHost

from bili_assistant import browser_login as login
from bili_assistant import browser_login_transport as transport
from bili_assistant import credentials
from bili_assistant.api import call
from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError

FIELDS = {"sessdata": "NEW_SESSION_123", "bili_jct": "NEW_CSRF_123", "dedeuserid": "123",
          "buvid3": "NEW_DEVICE_3", "buvid4": "NEW_DEVICE_4", "ac_time_value": "NEW_REFRESH_123"}


@pytest.fixture
def env(admin, monkeypatch):
    admin.stop(paused=True)
    admin.clock = lambda: datetime.now(timezone.utc)
    vault = {}
    monkeypatch.setattr(credentials.keyring, "get_password", lambda s, k: vault.get((s, k)))
    monkeypatch.setattr(credentials.keyring, "set_password", lambda s, k, v: vault.__setitem__((s, k), v))
    monkeypatch.setattr(credentials.keyring, "delete_password", lambda s, k: vault.pop((s, k), None))
    credentials.save(admin.store, Credential(sessdata="OLD_SESSION", bili_jct="OLD_CSRF",
                                           dedeuserid="123", ac_time_value="OLD_REFRESH"))
    profile = AsyncMock(return_value={"mid": 123, "name": "fixture"})
    monkeypatch.setattr(user, "get_self_info", profile)
    boxes = {}

    def crypt(raw, binding, *, decrypt=False):
        if decrypt:
            if raw not in boxes or boxes[raw][1] != binding:
                raise AssistantError("login_encryption_failed", "encrypted handoff rejected")
            return boxes[raw][0]
        opaque = f"encrypted-{len(boxes)}".encode()
        boxes[opaque] = (raw, binding)
        return opaque

    monkeypatch.setattr(transport, "crypt", crypt)
    monkeypatch.setattr(transport, "supported", lambda: True)
    return SimpleNamespace(admin=admin, store=admin.store, vault=vault, profile=profile, boxes=boxes)


def stage(env, fields=None):
    ticket = login.prepare(env.admin)
    raw = json.dumps({"origin": transport.ORIGIN, "credentials": FIELDS if fields is None else fields}).encode()
    transport.seal(env.store.root, ticket["ticket_id"], raw)
    return ticket


def secret_key(env):
    return credentials.SERVICE, credentials.key(env.store)


@pytest.mark.asyncio
async def test_admin_full_flow_and_repeat_finish_preserve_run_policy_and_account(env):
    before = {k: env.store.read(k) for k in ("manifest", "policy", "persona", "preferences", "companion")}
    run = env.store.state("run")
    ticket = stage(env)
    assert login.status(env.admin)["status"] == "staged"
    result = await call(env.admin, "login_browser_finish", {"ticket_id": ticket["ticket_id"]})
    assert result["status"] == "succeeded" and result["result"]["refresh_available"]
    assert result["result"]["replaced"] is True
    assert credentials.load(env.store).sessdata == FIELDS["sessdata"]
    assert json.loads(env.vault[(credentials.SERVICE, credentials.key(env.store) + ":previous")])["sessdata"] == "OLD_SESSION"
    assert await login.finish(env.admin, ticket["ticket_id"]) == result
    assert env.profile.await_count == 1
    assert env.store.state("run") == run
    assert all(env.store.read(k) == v for k, v in before.items())
    assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()
    # State, audit, ticket and serialized tool results contain neither new nor old secrets.
    output = json.dumps(result)
    for path in env.store.root.rglob("*"):
        if path.is_file():
            output += path.read_bytes().decode("utf-8", errors="ignore")
    assert all(v not in output for k, v in FIELDS.items() if k != "dedeuserid")
    assert "OLD_SESSION" not in output and "OLD_REFRESH" not in output


@pytest.mark.asyncio
async def test_role_and_another_admin_cannot_finish_or_see_ticket(env, role):
    ticket = stage(env)
    for name in ("login_browser_prepare", "login_browser_status", "login_browser_finish", "login_browser_cancel"):
        with pytest.raises(AssistantError, match="admin"):
            await call(role, name, {} if name == "login_browser_prepare" else {"ticket_id": ticket["ticket_id"]})
    other = Engine(env.store, FakeHost("other-admin"), clock=env.admin.clock)
    other.open("admin")
    with pytest.raises(AssistantError) as exc:
        await login.finish(other, ticket["ticket_id"])
    assert exc.value.code == "login_owner_mismatch"
    assert credentials.load(env.store).sessdata == "OLD_SESSION"


def test_prepare_requires_paused_and_bound_account(env, role):
    role.start()
    with pytest.raises(AssistantError) as exc:
        login.prepare(env.admin)
    assert exc.value.code == "login_requires_pause"
    role.stop(paused=True)
    manifest = env.store.read("manifest")
    manifest["data"]["account_uid"] = ""
    env.store.write("manifest", manifest["data"], manifest["version"], "test")
    with pytest.raises(AssistantError) as exc:
        login.prepare(env.admin)
    assert exc.value.code == "account_not_bound"


def test_prepare_reuses_valid_task_and_replaces_expired_without_role_start(env):
    first = login.prepare(env.admin)
    assert login.prepare(env.admin) == first
    env.admin.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=16)
    second = login.prepare(env.admin)
    assert second["ticket_id"] != first["ticket_id"]
    assert env.store.state("run")["status"] == "paused"


def test_prepare_recovers_after_runtime_task_metadata_was_cleaned(env):
    first = stage(env)
    transport.ticket_path(env.store.root, first["ticket_id"]).unlink()
    second = login.prepare(env.admin)
    assert second["ticket_id"] != first["ticket_id"]
    assert not transport.ticket_path(env.store.root, first["ticket_id"], ".sealed").exists()
    assert credentials.load(env.store).sessdata == "OLD_SESSION"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["run", "manifest"])
async def test_change_before_verification_fails_task_and_erases_ciphertext(env, change):
    ticket = stage(env)
    if change == "run":
        env.store.set_state("run", {**env.store.state("run"), "status": "active"})
    else:
        doc = env.store.read("manifest")
        env.store.write("manifest", {**doc["data"], "account_uid": "999"}, doc["version"], "test")
    with pytest.raises(AssistantError):
        await login.finish(env.admin, ticket["ticket_id"])
    assert login.status(env.admin)["status"] == "failed"
    assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()
    assert credentials.load(env.store).sessdata == "OLD_SESSION"
    env.profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["invalid_profile", "wrong_uid", "network_secret", "damaged_ciphertext"])
async def test_validation_failure_keeps_old_keyring_and_erases_ciphertext(env, reason):
    ticket = stage(env)
    if reason == "invalid_profile":
        env.profile.return_value = {"mid": "123", "name": "fixture"}
    elif reason == "wrong_uid":
        env.profile.return_value = {"mid": 999, "name": "other"}
    elif reason == "network_secret":
        env.profile.side_effect = RuntimeError(FIELDS["sessdata"])
    else:
        transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").write_bytes(b"tampered")
    with pytest.raises(AssistantError) as exc:
        await login.finish(env.admin, ticket["ticket_id"])
    assert FIELDS["sessdata"] not in str(exc.value)
    assert credentials.load(env.store).sessdata == "OLD_SESSION"
    assert login.status(env.admin)["status"] == "failed"
    assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "expire", "archive", "run", "manifest", "supersede"])
async def test_change_during_verification_revokes_save(env, change):
    ticket = stage(env)

    async def change_state(_):
        if change == "cancel":
            login.cancel(env.admin, ticket["ticket_id"])
        elif change == "expire":
            env.admin.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=16)
        elif change == "archive":
            env.admin.host.states["admin"] = True
        elif change == "run":
            env.store.set_state("run", {**env.store.state("run"), "status": "active"})
        elif change == "manifest":
            doc = env.store.read("manifest")
            env.store.write("manifest", {**doc["data"], "account_uid": "789"}, doc["version"], "test")
        else:
            other = Engine(env.store, FakeHost("other-admin"), clock=env.admin.clock)
            other.open("admin")
            login.prepare(other)
        return {"mid": 123, "name": "fixture"}

    env.profile.side_effect = change_state
    try:
        result = await login.finish(env.admin, ticket["ticket_id"])
        assert result["status"] in ("cancelled", "expired", "superseded")
    except AssistantError:
        pass
    assert env.vault[secret_key(env)] and json.loads(env.vault[secret_key(env)])["sessdata"] == "OLD_SESSION"
    assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()


@pytest.mark.asyncio
async def test_concurrent_finish_does_not_repeat_profile_or_write(env):
    ticket = stage(env)
    entered, release = asyncio.Event(), asyncio.Event()

    async def profile(_):
        entered.set()
        await release.wait()
        return {"mid": 123, "name": "fixture"}

    env.profile.side_effect = profile
    first = asyncio.create_task(login.finish(env.admin, ticket["ticket_id"]))
    await entered.wait()
    with pytest.raises(AssistantError) as exc:
        await login.finish(env.admin, ticket["ticket_id"])
    assert exc.value.code == "login_not_staged"
    release.set()
    assert (await first)["status"] == "succeeded"
    assert env.profile.await_count == 1


@pytest.mark.asyncio
async def test_optional_refresh_is_not_copied_from_previous_session(env):
    fields = {k: v for k, v in FIELDS.items() if k != "ac_time_value"}
    ticket = stage(env, fields)
    result = await login.finish(env.admin, ticket["ticket_id"])
    assert result["result"]["refresh_available"] is False
    assert credentials.load(env.store).ac_time_value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["backup", "write", "readback", "rollback"])
async def test_keyring_failures_preserve_old_or_report_uncertain(env, monkeypatch, failure):
    ticket = stage(env)
    original_get = credentials.keyring.get_password
    original_set = credentials.keyring.set_password
    damaged = False

    def write(service, key, value):
        nonlocal damaged
        if failure == "backup" and key.endswith(":previous"):
            raise RuntimeError("keyring backend error " + FIELDS["sessdata"])
        if key == secret_key(env)[1] and FIELDS["sessdata"] in value:
            damaged = True
            if failure in ("write", "rollback"):
                original_set(service, key, value)  # Simulate a write followed by an error.
                raise RuntimeError("failed after write")
        if failure == "rollback" and damaged and key == secret_key(env)[1]:
            raise RuntimeError("rollback failed")
        original_set(service, key, value)

    def read(service, key):
        value = original_get(service, key)
        if failure == "readback" and value and FIELDS["sessdata"] in value:
            return "invalid"
        return value

    monkeypatch.setattr(credentials.keyring, "set_password", write)
    monkeypatch.setattr(credentials.keyring, "get_password", read)
    with pytest.raises(AssistantError) as exc:
        await login.finish(env.admin, ticket["ticket_id"])
    assert FIELDS["sessdata"] not in str(exc.value)
    result = login.status(env.admin)
    assert result["status"] == ("uncertain" if failure == "rollback" else "failed")
    if failure != "rollback":
        assert credentials.load(env.store).sessdata == "OLD_SESSION"
    assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()


def test_seal_rejects_wrong_origin_account_missing_devices_extra_fields_and_oversize(env):
    ticket = login.prepare(env.admin)
    for body in (
        {"origin": "https://bilibili.com.attacker.invalid", "credentials": FIELDS},
        {"origin": transport.ORIGIN, "credentials": {**FIELDS, "dedeuserid": "456"}},
        {"origin": transport.ORIGIN, "credentials": {k: v for k, v in FIELDS.items() if k != "buvid4"}},
        {"origin": transport.ORIGIN, "credentials": {**FIELDS, "proxy": "secret"}},
        {"origin": transport.ORIGIN, "credentials": {**FIELDS, "sessdata": "secret\r\nbad"}},
    ):
        with pytest.raises(AssistantError):
            transport.seal(env.store.root, ticket["ticket_id"], json.dumps(body).encode())
    with pytest.raises(AssistantError):
        transport.seal(env.store.root, ticket["ticket_id"], b"x" * (transport.MAX_INPUT + 1))
    assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()


def test_cancel_and_expiry_cleanup_and_prevent_reuse(env):
    ticket = stage(env)
    login.cancel(env.admin, ticket["ticket_id"])
    raw = json.dumps({"origin": transport.ORIGIN, "credentials": FIELDS}).encode()
    with pytest.raises(AssistantError):
        transport.seal(env.store.root, ticket["ticket_id"], raw)
    second = stage(env)
    env.admin.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=16)
    assert login.status(env.admin, second["ticket_id"])["status"] == "expired"
    assert not transport.ticket_path(env.store.root, second["ticket_id"], ".sealed").exists()


@pytest.mark.asyncio
async def test_secret_arguments_and_path_traversal_are_rejected_before_dispatch(env):
    with pytest.raises(AssistantError) as exc:
        await call(env.admin, "login_browser_finish", {"ticket_id": "../manifest", "cookie": FIELDS})
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(AssistantError):
        transport.ticket_path(env.store.root, "..\\manifest")
    assert env.profile.await_count == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_real_dpapi_binds_to_task_and_hides_plaintext():
    raw = b"synthetic-session-for-test-only"
    encrypted = transport.crypt(raw, b"task-1")
    assert raw not in encrypted
    assert transport.crypt(encrypted, b"task-1", decrypt=True) == raw
    with pytest.raises(AssistantError):
        transport.crypt(encrypted, b"other-task", decrypt=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows standalone handoff integration")
def test_standalone_sealer_with_real_dpapi_has_no_asyncio_or_secret_output(env):
    # The child imports the real crypto implementation and must finish without an event loop.
    ticket = login.prepare(env.admin)
    raw = json.dumps({"origin": transport.ORIGIN, "credentials": FIELDS})
    result = subprocess.run(
        [sys.executable, "-m", "bili_assistant.browser_login_transport",
         "--data", str(env.store.root), "--ticket", ticket["ticket_id"]],
        input=raw, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "staged"
    assert FIELDS["sessdata"] not in result.stdout + result.stderr
    encrypted = transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").read_bytes()
    assert FIELDS["sessdata"].encode() not in encrypted


@pytest.mark.asyncio
async def test_interrupted_save_is_reported_uncertain_without_replay(env):
    ticket = stage(env)
    stored = transport.read_ticket(env.store.root, ticket["ticket_id"])
    stored["status"] = "saving"
    transport.write_ticket(env.store.root, stored)
    assert login.status(env.admin)["status"] == "uncertain"
    assert (await login.finish(env.admin, ticket["ticket_id"]))["status"] == "uncertain"
    env.profile.assert_not_awaited()
    assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()


@pytest.mark.skipif(os.name != "nt" or not shutil.which("node"), reason="Windows/Node browser bridge integration")
@pytest.mark.parametrize("scenario", [
    "success", "reordered", "wrong_origin", "navigated", "wrong_account", "missing_device",
    "changed_session", "conflicting_cookies", "stale_runtime", "wrong_store", "wrong_project",
])
def test_packaged_js_bridge_with_isolated_runtime(env, tmp_path, scenario):
    project = Path(__file__).resolve().parents[1]
    plugin = tmp_path / "isolated-plugin"
    (plugin / "scripts").mkdir(parents=True)
    shutil.copyfile(project / "plugins/qiban/scripts/browser-login.mjs",
                    plugin / "scripts/browser-login.mjs")
    (plugin / "runtime.json").write_text(json.dumps({
        "python": sys.executable, "project": str(project),
        "data": str(tmp_path / 'stale-data') if scenario == 'stale_runtime' else str(env.store.root),
    }), encoding="utf-8")
    ticket = login.prepare(env.admin)
    assert ticket['handoff']['data'] == str(env.store.root.resolve())
    if scenario == 'wrong_store':
        ticket['handoff']['data'] = str(tmp_path / 'wrong-data')
    elif scenario == 'wrong_project':
        ticket['handoff']['project_id'] = 'different-project'
    ticket_file = tmp_path / "public-ticket.json"
    ticket_file.write_text(json.dumps(ticket), encoding="utf-8")
    result = subprocess.run([
        shutil.which("node"), str(project / "tests/browser_login_bridge.mjs"),
        str(plugin / "scripts/browser-login.mjs"), str(ticket_file), scenario,
    ], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert all(v not in result.stdout + result.stderr for k, v in FIELDS.items() if k != "dedeuserid")
    if scenario in ("success", "reordered", "stale_runtime"):
        assert result.returncode == 0
        assert json.loads(result.stdout)["status"] == "staged"
        assert transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").is_file()
    else:
        assert result.returncode == 2
        assert not transport.ticket_path(env.store.root, ticket["ticket_id"], ".sealed").exists()
        if scenario in ("wrong_origin", "wrong_store", "wrong_project"):
            assert json.loads(result.stdout)["cookieReads"] == 0
