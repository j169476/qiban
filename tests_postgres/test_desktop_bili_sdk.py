"""The production adapter and desktop database; synthetic Bilibili transport only."""
from datetime import timedelta

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_bili_connector import adapter
from qiban.desktop_profile import desktop_profile
from qiban.desktop_runtime import DesktopRuntime
from tests_postgres.test_bili_governance import NOW, Model, Platform, patch
from tests_postgres.test_desktop_bili import login
from tests_postgres.test_desktop_management import call, desktop_store, service

__all__ = ["desktop_store","service","login"]


@pytest.fixture
def pg_profile():
    return desktop_profile()


def test_pre_sdk_backup_restores_into_desktop_profile(service):
    from companion_core.postgres_transfer import create, fingerprint, restore
    from companion_core.profiles import default_profile

    store = service.store
    desktop = store.profile
    try:
        store.profile = default_profile()
        old = create(store,store._backup_key)
    finally:
        store.profile = desktop
    with store.db() as db:
        db.execute("INSERT INTO videos VALUES (?,?,?)",("123","2026-11-07","fixture"))
    result = restore(store,old,store._backup_key,expected=fingerprint(store))
    assert result["restored"] and not result["role_started"]
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM videos").fetchone()[0]==0


async def test_production_adapter_uses_bound_cookie_and_runs_without_chat(service,login):
    broker,op,start,_ = login
    start("123","合成账号")
    connector = adapter(broker)
    provider = connector.engine.adapter.credential_provider
    revision = provider.revision()
    credential = provider.load()
    assert credential.dedeuserid=="123" and credential.sessdata=="synthetic-secret"
    credential.sessdata="synthetic-renewed"
    provider.save_refresh(credential,revision)
    assert provider.load().sessdata=="synthetic-renewed"
    platform = Platform()
    platform.incoming = platform.incoming[:1]
    connector.engine.adapter = platform
    model = Model()
    patch(service.store,"companion",memory_maintenance=False,daily_reflection=False,
          knowledge_review=False,search_expansion=False,media_fallback="off")
    runtime = DesktopRuntime(service,"sdk-activity",room_model=model,adapters=[connector])
    core = runtime.cores["bilibili"]
    core.clock=connector.engine.clock=lambda:NOW-timedelta(seconds=1)
    try:
        assert not runtime.reconcile()["bilibili"]["started"] and not platform.sent
        assert call(service,"runtime.control",{"platform":"bilibili","action":"start_read_only"})["ok"]
        assert core.guard()["read_only"]
        with pytest.raises(AssistantError) as disabled:
            connector.engine.prepare("dynamic",{"text":"只读验证"})
        assert disabled.value.code=="action_disabled"
        runtime.control(service.request(),"bilibili","stop")
        runtime.start("bilibili")
        await core.cycle()
        assert [kind for kind,_ in platform.sent]==["dm_reply"]
        assert all("synthetic-secret" not in str(context) and "synthetic-renewed" not in str(context) for context in model.calls)
        runtime.control(service.request(),"bilibili","stop")
        pending=start("202","新账号")
        op("confirm",ticket=pending["ticket"])
        with pytest.raises(AssistantError):
            provider.save_refresh(credential,revision)
        assert adapter(broker).engine.adapter.credential_provider.load().dedeuserid=="202"
    finally:
        runtime.close()
