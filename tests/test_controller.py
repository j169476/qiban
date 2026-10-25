from datetime import timedelta

import pytest
from conftest import FakeHost

from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError


def test_role_cannot_modify_policy(role):
    old = role.store.read("policy")
    with pytest.raises(AssistantError, match="admin"):
        role.config_patch("policy", {"daily_videos": 99}, old["version"])


def test_admin_cannot_write_memory(admin, memory):
    with pytest.raises(AssistantError, match="role"):
        admin.remember("memories", memory)


def test_mode_immutable(admin):
    with pytest.raises(AssistantError, match="其他模式"):
        admin.start()


def test_repeat_start_and_takeover(role):
    first = role.run()
    assert role.start()["run"]["generation"] == first["generation"]
    second = Engine(role.store, FakeHost("new", role.host.states), role.adapter, role.clock)
    result = second.start()["run"]
    assert first["generation"] != result["generation"]
    role.host.states["role"] = True
    assert second.run()["status"] == "active"
    assert not role.stop_run("old_hook", first["generation"])["stopped"]


def test_admin_archive_does_not_stop_role(role, admin):
    role.host.states["admin"] = True
    assert role.run()["status"] == "active"


def test_role_archive_stops_generation(role):
    role.host.states["role"] = True
    with pytest.raises(AssistantError, match="archived"):
        role.run()
    assert role.store.state("run")["status"] == "paused"
    role.host.states["role"] = False
    with pytest.raises(AssistantError):
        role.run()
    assert role.start()["run"]["status"] == "active"


def test_stop_both_modes_and_no_resume_by_config(role, admin):
    admin.stop()
    old = admin.config_get()
    admin.config_patch("policy", {"daily_videos": 50}, old["version"])
    assert role.store.state("run")["status"] == "stopped"


def test_memory_dedupe(role, memory):
    role.remember("memories", memory)
    memory["id"] = "m2"
    assert role.remember("memories", memory)["duplicate"]
    assert len(role.store.records("memories")[0]) == 1


def test_relationship_limits_and_dedupe(role):
    r = role.relationship("789", "好友", 3, "real-event-1")["data"]
    assert r["affinity"] == 53 and r["interactions"] == 1
    assert role.relationship("789", "好友", 3, "real-event-1")["duplicate"]
    with pytest.raises(AssistantError):
        role.relationship("789", "好友", 4, "event2")


@pytest.mark.asyncio
async def test_action_idempotency(role):
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    assert (await role.execute(a["id"]))["status"] == "succeeded"
    assert not (await role.execute(a["id"]))["sent"]
    assert role.prepare("like", {"bvid": "BV1xx411c7mD"})["id"] == a["id"]
    assert len(role.adapter.sent) == 1


@pytest.mark.asyncio
async def test_unknown_result_never_retried(role):
    role.adapter.fail = True
    a = role.prepare("dynamic", {"text": "一次有内容的记录"})
    assert (await role.execute(a["id"]))["status"] == "uncertain"
    assert not (await role.execute(a["id"]))["sent"]
    assert len(role.adapter.sent) == 1
    assert role.status()["quota"]["dynamic"]["used"] == 1


@pytest.mark.asyncio
async def test_reduced_quota_preserves_usage(role, admin):
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    await role.execute(a["id"])
    pending = role.prepare("like", {"bvid": "BV1xx411c7mE"})
    old = admin.config_get()
    admin.config_patch("policy", {"actions": {"like": {"daily_limit": 0}}}, old["version"])
    with pytest.raises(AssistantError, match="额度"):
        await role.execute(pending["id"])
    assert admin.status()["quota"]["like"] == {"used": 1, "limit": 0, "remaining": 0}
    assert len(role.adapter.sent) == 1


@pytest.mark.asyncio
async def test_disabled_after_prepare(role, admin):
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    old = admin.config_get()
    admin.config_patch("policy", {"actions": {"like": {"enabled": False}}}, old["version"])
    with pytest.raises(AssistantError, match="关闭"):
        await role.execute(a["id"])
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_unknown_quota_shared_across_chats(role):
    role.adapter.fail = True
    await role.execute(role.prepare("dynamic", {"text": "first"})["id"])
    second = Engine(role.store, FakeHost("new", role.host.states), role.adapter, role.clock)
    second.start()
    with pytest.raises(AssistantError, match="额度"):
        await second.execute(second.prepare("dynamic", {"text": "second"})["id"])


@pytest.mark.asyncio
async def test_day_rollover(role):
    await role.execute(role.prepare("dynamic", {"text": "day1"})["id"])
    day_one = role.clock()
    role.clock = lambda: day_one + timedelta(days=1)
    assert (await role.execute(role.prepare("dynamic", {"text": "day2"})["id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_archive_prevents_pending_send(role):
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    role.host.states["role"] = True
    with pytest.raises(AssistantError):
        await role.execute(a["id"])
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_account_mismatch(role):
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    role.adapter.uid = "999"
    with pytest.raises(AssistantError, match="UID"):
        await role.execute(a["id"])
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_comment_requires_actual_content(role):
    a = role.prepare("comment", {"bvid": "BV1xx411c7mD", "text": "关于视频内容的评论"})
    with pytest.raises(AssistantError, match="实际视频"):
        await role.execute(a["id"])
    role.store.set_state("context:BV1xx411c7mD", {"coverage": {"kind": "subtitles"}, "owner_uid": "888"})
    assert (await role.execute(a["id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_no_unprompted_dm(role):
    a = role.prepare("dm_reply", {"event_id": "invented", "text": "hello"})
    with pytest.raises(AssistantError, match="实际收到"):
        await role.execute(a["id"])
    assert not role.adapter.sent


def test_end_marker_does_not_reuse_old_generation(role):
    old = role.run()
    (role.store.runtime / f"stop-{old['generation']}").write_text("session_end")
    assert role.start()["run"]["generation"] != old["generation"]


@pytest.mark.asyncio
async def test_invalid_policy_blocks_dispatch(role):
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    role.store.path("policy").write_text("broken: [")
    with pytest.raises(AssistantError):
        await role.execute(a["id"])
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_like_respects_blocked_video_author(role, admin):
    prefs = admin.config_get("preferences")
    admin.config_patch("preferences", {"blocked_uids": ["888"]}, prefs["version"])
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    with pytest.raises(AssistantError, match="排除"):
        await role.execute(a["id"])
    assert not role.adapter.sent
