import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from bilibili_api import Credential, user

from bili_assistant import credentials
from bili_assistant.errors import AssistantError


@pytest.fixture
def vault(store, monkeypatch):
    values = {}
    monkeypatch.setattr(credentials.keyring, "get_password", lambda service, key: values.get((service, key)))
    monkeypatch.setattr(credentials.keyring, "set_password",
                        lambda service, key, raw: values.__setitem__((service, key), raw))
    credentials.save(store, Credential(sessdata="old-session", bili_jct="old-csrf",
                                      dedeuserid="123", ac_time_value="old-refresh"))
    monkeypatch.setattr(user, "get_self_info", AsyncMock(return_value={"mid": 123, "name": "fixture"}))
    return values


@pytest.mark.asyncio
async def test_refresh_only_when_due_verifies_uid_then_saves_keyring(store, vault, monkeypatch):
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(Credential, "check_refresh", check)
    changed = []

    async def renew(credential):
        changed.append(1)
        credential.sessdata = "new-session"
        credential.bili_jct = "new-csrf"
        credential.ac_time_value = "new-refresh"

    monkeypatch.setattr(Credential, "refresh", renew)
    now = datetime.now(timezone.utc)
    result = await credentials.maintain(store, "123", now)
    assert result["status"] == "refreshed" and credentials.load(store).sessdata == "new-session"
    assert user.get_self_info.await_count == 2
    await credentials.maintain(store, "123", now + timedelta(hours=1))
    assert len(changed) == 1
    raw_state = json.dumps(store.state("credential-health:123"))
    assert all(secret not in raw_state for secret in ("new-session", "new-csrf", "new-refresh"))


@pytest.mark.asyncio
async def test_valid_cookie_without_refresh_token_does_not_force_relogin(store, vault, monkeypatch):
    credential = credentials.load(store)
    credential.ac_time_value = None
    credentials.save(store, credential)
    monkeypatch.setattr(Credential, "check_refresh", AsyncMock(return_value=False))
    renew = AsyncMock()
    monkeypatch.setattr(Credential, "refresh", renew)
    result = await credentials.maintain(store, "123")
    assert result["status"] == "valid" and not result["refresh_available"]
    renew.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_missing_token_and_invalid_account_never_write(store, vault, monkeypatch):
    credential = credentials.load(store)
    credential.ac_time_value = None
    credentials.save(store, credential)
    before = dict(vault)
    monkeypatch.setattr(Credential, "check_refresh", AsyncMock(return_value=True))
    with pytest.raises(AssistantError) as error:
        await credentials.maintain(store, "123")
    assert error.value.code == "credential_refresh_required"
    user.get_self_info.return_value = {"mid": 999}
    with pytest.raises(AssistantError) as error:
        await credentials.maintain(store, "123")
    assert error.value.code == "account_mismatch" and vault == before


@pytest.mark.asyncio
async def test_ambiguous_refresh_is_never_retried_and_secrets_are_sanitized(store, vault, monkeypatch):
    monkeypatch.setattr(Credential, "check_refresh", AsyncMock(return_value=True))
    renew = AsyncMock(side_effect=TimeoutError("fake-secret-refresh"))
    monkeypatch.setattr(Credential, "refresh", renew)
    for _ in range(2):
        with pytest.raises(AssistantError) as error:
            await credentials.maintain(store, "123")
        assert error.value.code == "credential_refresh_uncertain"
        assert "fake-secret" not in str(error.value)
    assert renew.await_count == 1
    assert credentials.load(store).sessdata == "old-session"


@pytest.mark.asyncio
async def test_manifest_edit_during_refresh_preserved_and_stale_credentials_not_saved(store, vault, monkeypatch):
    monkeypatch.setattr(Credential, "check_refresh", AsyncMock(return_value=True))
    before = dict(vault)

    async def change_manifest(credential):
        credential.sessdata = "new-session"
        doc = store.read("manifest")
        doc["data"]["account_uid"] = "456"
        store.write("manifest", doc["data"], doc["version"], "manual")

    monkeypatch.setattr(Credential, "refresh", change_manifest)
    with pytest.raises(AssistantError):
        await credentials.maintain(store, "123")
    assert store.read("manifest")["data"]["account_uid"] == "456"
    assert vault == before


@pytest.mark.asyncio
async def test_read_network_failure_is_retryable_not_permanent_refresh_failure(store, vault, monkeypatch):
    user.get_self_info.side_effect = TimeoutError("fake-cookie")
    with pytest.raises(AssistantError) as error:
        await credentials.maintain(store, "123")
    assert error.value.code == "credential_check_failed"
    assert not store.state("credential-health:123")
    user.get_self_info.side_effect = None
    monkeypatch.setattr(Credential, "check_refresh", AsyncMock(return_value=False))
    assert (await credentials.maintain(store, "123"))["status"] == "valid"
