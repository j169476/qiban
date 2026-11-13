"""Dynamic scopes require both explicit account/category consent and current platform membership."""

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience, LiveMembers, change, preview, read, registry
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from tests_postgres.test_desktop_management import desktop_store

__all__ = ["desktop_store"]


def set_mode(store, category, enabled):
    prompt = preview(store, category, enabled)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        return change(
            store, category, enabled, prompt["expected"], actor="fixture", confirmed=prompt["scope"], db=db
        )


async def test_wechat_directory_requires_the_complete_enabled_category(desktop_store):
    from companion_wechat.membership import FollowDirectory, MembershipCheck
    store = desktop_store
    directory = FollowDirectory(store)
    category = Audience("wechat", "test-account", "private")
    scope = ReplyScope("wechat", category.account_identity, category.kind, "peer")
    permissions = ReplyAuthorizations(store)
    class Worker:
        calls = 0
        fail = False
        async def request(self, command, *, kind, cursor):
            self.calls += 1
            assert command == "member_page" and kind == "private"
            if cursor is None:
                return {"ids": [f"id-{i}" for i in range(256)], "next":"c"*32}
            assert cursor == "c"*32
            if self.fail:
                raise RuntimeError("synthetic disconnect")
            return {"ids":["peer"], "next":None}
    worker = Worker()
    async def refresh():
        await directory.refresh(category.account_identity, worker, lambda:True)
    await refresh()
    assert worker.calls == 0
    set_mode(store,category,True)
    await refresh()
    original = permissions.version(scope)
    assert original
    await refresh()
    assert permissions.version(scope) == original
    MembershipCheck(store, category.account_identity, [{"kind":"private","username":"peer"}]).finish(
        {"verified":0,"unavailable":["peer"]})
    assert permissions.version(scope) is None
    await refresh()
    assert permissions.version(scope) not in (original,None)
    worker.fail = True
    with pytest.raises(RuntimeError):
        await refresh()
    assert permissions.version(scope) is None and category not in registry(store)
    assert permissions.version(ReplyScope("wechat","test-account","private","id-0")) is None


@pytest.mark.parametrize("platform", ["qq", "wechat"])
@pytest.mark.parametrize("kind", ["private", "group"])
def test_all_is_scoped_and_removed_members_cannot_finish(desktop_store, platform, kind):
    store = desktop_store
    rules = ReplyAuthorizations(store)
    category = Audience(platform, "account-a", kind)
    target = ReplyScope(platform, "account-a", kind, "12345")
    others = [
        ReplyScope(platform, "account-b", kind, "12345"),
        ReplyScope(platform, "account-a", "private" if kind == "group" else "group", "12345"),
        ReplyScope("qq" if platform == "wechat" else "wechat", "account-a", kind, "12345"),
    ]
    prompt = preview(store, category, True)
    assert "高风险" in prompt["message"] and "account-a" in prompt["message"] and "额度" in prompt["message"]
    assert ("群成员" in prompt["message"]) == (kind == "group")
    assert not read(store, category)["enabled"]
    with pytest.raises(AssistantError), store.db() as db:
        change(
            store,
            category,
            True,
            None,
            actor="fixture",
            confirmed={**prompt["scope"], "kind": "other"},
            db=db,
        )
    policy = set_mode(store, category, True)
    live = registry(store)[category] = LiveMembers()
    assert rules.version(target) is None
    live.replace(policy["version"], {"12345": "uid-1"})
    pending = rules.version(target)
    assert pending and all(rules.version(other) is None for other in others)
    live.replace(policy["version"], {"12345": "uid-1", "23456": "uid-2"})
    rules.check(target, pending)
    live.replace(policy["version"], {"23456": "uid-2"})
    with pytest.raises(AssistantError), rules.submission(target, pending):
        pytest.fail("removed member reached submission")
    live.replace(policy["version"], {"12345": "uid-1"})
    assert rules.version(target) != pending
    live.disconnect()
    assert rules.version(target) is None


def test_toggle_invalidates_inflight_grants_and_restore_drops_broad_consent(desktop_store):
    store = desktop_store
    category = Audience("qq", "account", "private")
    target = ReplyScope("qq", "account", "private", "12345")
    rules = ReplyAuthorizations(store)
    manual = rules.update(target, enabled=True, expected_version=None, actor="test")
    first = set_mode(store, category, True)
    live = registry(store)[category] = LiveMembers()
    live.replace(first["version"], {"12345": "uid"})
    dynamic = rules.version(target)
    assert dynamic and dynamic != manual["version"]
    set_mode(store, category, False)
    assert rules.version(target) and rules.version(target) not in (dynamic, manual["version"])
    set_mode(store, category, True)
    assert rules.version(target) is None
    with pytest.raises(AssistantError), store.db() as db:
        change(store, category, False, first["version"], actor="test", confirmed=None, db=db)
    rules.reset_after_restore()
    assert not read(store, category)["enabled"] and rules.version(target) is None
