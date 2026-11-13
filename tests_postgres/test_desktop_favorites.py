"""Desktop account policy + production action ledger, synthetic Bilibili only."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bili_assistant.action_quota import daily_usage
from bili_assistant.errors import AssistantError
from bili_assistant.favorites import FavoriteTools
from qiban.desktop_bili_connector import adapter
from qiban.desktop_profile import desktop_profile
from qiban.desktop_runtime import DesktopRuntime
from tests.test_favorite_tools import BV1, BV2, kit, row
from tests_postgres.test_desktop_bili import login
from tests_postgres.test_desktop_management import desktop_store, service

__all__ = ["kit", "desktop_store", "service", "login"]


@pytest.fixture
def pg_profile():
    return desktop_profile()


def test_previous_nine_action_settings_gain_independent_defaults(service, login):
    _, op, start, _ = login
    start("123", "旧配置")
    value = op("policy", uid="123")["result"]
    old = {k: v for k, v in value["actions"].items() if k not in ("favorite_move", "favorite_create")}
    old["favorite"]["daily_limit"] = 17
    service.store.set_state("desktop-bili-policy:123", {"revision": "previous", "actions": old})
    current = op("policy", uid="123")["result"]
    assert current["actions"]["favorite"]["daily_limit"] == 17
    assert current["actions"]["favorite_create"]["daily_limit"] == 5
    assert current["actions"]["favorite_move"]["daily_limit"] == 50
    current["actions"]["favorite_create"]["daily_limit"] = 7
    current["actions"]["favorite_move"]["daily_limit"] = 24
    saved = op("policy_save", uid="123", expected=current["revision"], actions=current["actions"])
    assert saved["ok"] and saved["result"]["revision"] != "previous"
    assert op("policy", uid="123")["result"]["actions"] == current["actions"]


async def test_desktop_mutations_cancel_on_edit_and_preserve_used_quota(service, login, kit, monkeypatch):
    broker, op, start, _ = login
    start("123", "模拟收藏账号")
    connector = adapter(broker)
    connector.engine.adapter = kit.adapter
    monkeypatch.setattr(kit.adapter, "me", AsyncMock(return_value={"uid": "123"}))
    runtime = DesktopRuntime(service, "favorites-139", room_model=object(), adapters=[connector])
    sent = []
    async def post(operation, data, guard):
        if data.get("title") == "中途关闭":
            edit = op("policy", uid="123")["result"]
            edit["actions"]["favorite_create"]["enabled"] = False
            assert op("policy_save", uid="123", expected=edit["revision"], actions=edit["actions"])["ok"]
        guard()
        sent.append(operation)
        if operation == "new":
            kit.rows.append(row(30, title=data["title"], attr=2 + data["privacy"]))
            return {"id": 30}
        kit.states[BV1] = kit.states[BV2] = {20}
        return {}
    monkeypatch.setattr(FavoriteTools, "_post", lambda self, *args: post(*args))
    try:
        runtime.start("bilibili")
        engine = connector.engine
        kit.adapter.store = engine.store
        clock = [datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)]
        engine.clock = lambda: clock[0]
        async def execute(kind, args):
            clock[0] += timedelta(minutes=6)
            action = engine.prepare(kind, args)
            return await engine.execute(action["id"])
        result = await execute("favorite_create", {"title": "中途关闭", "private": True})
        assert result["status"] == "cancelled" and not sent
        edit = op("policy", uid="123")["result"]
        assert edit["used"]["favorite_create"] == 0
        edit["actions"]["favorite_create"]["enabled"] = True
        assert op("policy_save", uid="123", expected=edit["revision"], actions=edit["actions"])["ok"]
        assert (await execute("favorite_create", {"title": "新分类", "private": False}))["status"] == "succeeded"
        assert (await execute("favorite_move", {"source": 10, "target": 20, "bvids": [BV1, BV2]}))["status"] == "succeeded"
        edit = op("policy", uid="123")["result"]
        # Use the reservation's date, so this remains valid near local midnight.
        with engine.store.db() as db:
            day = db.execute("SELECT day FROM actions WHERE kind='favorite_move'").fetchone()["day"]
            assert daily_usage(db, "123", day)["favorite_move"] == 2
        edit["actions"]["favorite_move"]["daily_limit"] = 1
        assert op("policy_save", uid="123", expected=edit["revision"], actions=edit["actions"])["ok"]
        with pytest.raises(AssistantError) as error:
            await execute("favorite_move", {"source": 20, "target": 10, "bvids": [BV1]})
        assert error.value.code == "quota_exhausted"
        assert sent == ["new", "content_move"]
        runtime.control(service.request(), "bilibili", "stop")
        pending = start("202", "另一账号")
        assert op("confirm", ticket=pending["ticket"])["ok"]
        assert op("policy", uid="202")["result"]["used"]["favorite_move"] == 0
        assert op("policy", uid="202")["result"]["actions"]["favorite_move"]["daily_limit"] == 50
    finally:
        runtime.close()
