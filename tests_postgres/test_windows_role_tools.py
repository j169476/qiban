"""Windows 1.1 contracts and normal reply/dispatch entry points, using synthetic transports."""
import asyncio
from datetime import timedelta

import pytest

from companion_core import expression_learning, group_modes, reminder_runner, reminders
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from qiban.desktop_management import DesktopManagement, provision_owner
from tests.test_qq_native_normalization import normalize
from tests_postgres.matter_fixture import bound_turn, legacy_alarm
from tests_postgres.test_desktop_reply_quota import (
    INSTALLATION,
    SID,
    denied,
    qq_registered,
    result,
    wechat_registered,
)
from tests_postgres.test_qq_module import env as env
from tests_postgres.test_qq_module import handle, incoming


def manager(store, clock):
    provision_owner(store, INSTALLATION, SID)
    return DesktopManagement(store, INSTALLATION, SID, clock=clock)


def test_directory_group_modes_and_scoped_capabilities(pg_store):
    from qiban.desktop_role_tools import remember_directory
    api = manager(pg_store, lambda: 1792922390)
    qq_registered(pg_store)
    wechat_registered(pg_store)
    remember_directory(pg_store, "qq", "10001", {"12345": "合成旁听群"}, complete=True)
    base = {"platform": "qq", "account": "10001", "target": "12345"}
    before = result(api, "group_modes.list", {**base, "after": None})["items"][0]
    assert before["version"] is None and not before["reply_authorized"]
    after = result(api, "group_modes.save", {**base, "expected": None, "listen": True, "proactive": False})
    assert after["listen"] and not after["proactive"] and not after["reply_authorized"]
    assert denied(api, "group_modes.save", {**base, "expected": None, "listen": False, "proactive": False}) == "edit_conflict"
    assert not result(api, "group_modes.save", {**base, "expected": after["version"], "listen": False, "proactive": True})["proactive"]
    wx = {"platform": "wechat", "account": "wxid_synthetic", "target": "999@chatroom"}
    assert result(api, "group_modes.list", {**wx, "after": None})["items"][0]["capabilities"]["listen"]
    assert denied(api, "group_modes.save", {**wx, "expected": None, "listen": True, "proactive": True}) == "group_source_unavailable"


@pytest.mark.asyncio
async def test_passive_learning_and_independent_initiative(env):
    group = ReplyScope("qq", "10001", "group", "10003")
    auth = ReplyAuthorizations(env.store)
    auth.update(group, enabled=False, expected_version=auth.version(group), actor="synthetic")
    stamp = env.core.clock().timestamp()
    with env.store.db() as db:
        setting = group_modes.save(env.store, group, None, True, False, stamp - 1, db)
    for n in (801, 802):
        assert not await handle(env, incoming(n, group=True, text="我在听，私事甲不对外讲"))
    assert not env.provider.sent and not env.model.calls
    shared = expression_learning.context(env.store)
    assert shared and "私事甲" not in str(shared)
    auth.update(group, enabled=True, expected_version=auth.record(group)["version"], actor="synthetic")
    raw = incoming(803, group=True, text="想请大家听一下")
    actor = normalize(raw).actor
    env.store.write("relationships", {"schema_version": 1, "id": actor.key, "display_name": "合成熟人",
        "affinity": 85, "interactions": 4, "events": [], "locked": False}, None, "test", actor.key)
    card = env.store.read("persona")
    assert not await handle(env, raw)
    with env.store.db() as db:
        group_modes.save(env.store, group, setting["version"], True, True, stamp - 1, db)
    original = env.model.generate
    async def proposes_increase(prompt, context, schema, *args, **opts):
        assert context["event_material"]["mode"] == "voluntary"
        value = await original(prompt, context, schema, *args, **opts)
        return {**value, "relationship": {"delta": 3, "impression": "只是我刚主动打了招呼", "facts": []}}
    env.model.generate = proposes_increase
    await handle(env, incoming(804, group=True, text="想请大家听一下"))
    assert len(env.provider.sent) == 1
    assert not await handle(env, incoming(805, group=True))
    assert env.store.read("persona") == card
    assert env.store.read("relationships", actor.key)["data"]["affinity"] == 85
    assert shared == expression_learning.context(env.store)


async def setup_reminder(env, n, text="明天提醒我带报名材料", source="explicit"):
    await handle(env, incoming(n, text=text))
    return legacy_alarm(env.store,normalize(incoming(n)),env.core.clock()+timedelta(minutes=1),"带报名材料",source)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["send", "complete", "uncertain"])
async def test_due_uses_recent_chat_and_never_double_sends(env, outcome):
    alarm = await setup_reminder(env, 811)
    text = "报名材料已经交了" if outcome == "complete" else "我在准备材料"
    await handle(env, incoming(812, text=text))
    before = len(env.provider.sent)
    saved = env.core.clock()
    env.core.clock = lambda: saved + timedelta(minutes=2)
    async def model(instructions, context, schema, *args, **kwargs):
        assert text in str(context["conversation"])
        assert context["clue"] == "带报名材料"
        return bound_turn(None if outcome == "complete" else "记得带报名材料。")
    env.model.generate = model
    env.provider.unknown = outcome == "uncertain"
    await asyncio.gather(reminder_runner.tick(env.core, env.module), reminder_runner.tick(env.core, env.module))
    row = reminders.listing(env.store, id=alarm["id"])["items"][0]
    assert row["status"] == {"send": "sent", "complete": "completed", "uncertain": "uncertain"}[outcome]
    assert len(env.provider.sent) == before + (outcome != "complete")
    if outcome == "uncertain":
        env.module.tainted = False
    await reminder_runner.tick(env.core, env.module)
    assert len(env.provider.sent) == before + (outcome != "complete")


@pytest.mark.asyncio
async def test_admin_reschedule_cancel_and_versioned_race(env):
    clock = env.core.clock()
    api = manager(env.store, lambda: clock.timestamp())
    alarm = await setup_reminder(env, 821)
    when = (clock + timedelta(days=2)).isoformat()
    moved = result(api, "reminders.reschedule", {"id": alarm["id"], "expected": alarm["version"], "due_at": when})
    env.core.clock = lambda: clock + timedelta(minutes=2)
    await reminder_runner.tick(env.core, env.module)
    assert len(env.provider.sent) == 1
    assert denied(api, "reminders.cancel", {"id": alarm["id"], "expected": alarm["version"]}) == "edit_conflict"
    cancelled = result(api, "reminders.cancel", {"id": alarm["id"], "expected": moved["version"]})
    assert cancelled["status"] == "cancelled" and not result(api, "reminders.list", {"after": None, "status": "pending"})["items"]


@pytest.mark.asyncio
async def test_restart_recovers_claim_and_quarantines_lost_receipt(env):
    alarm = await setup_reminder(env, 831)
    now = env.core.clock().timestamp() + 120
    first = reminder_runner.claim(env.store, env.module, now)
    assert first and not reminder_runner.claim(env.store, env.module, now + 1)
    job, attempt = reminder_runner.claim(env.store, env.module, now + reminder_runner.LEASE + 1)
    assert attempt != first[1] and job["id"] == alarm["id"]
    with env.store.db() as db:
        db.execute("INSERT INTO core_actions(id,dedupe,generation,platform,account,capability,body,status,created) "
                   "VALUES(?,?,'old','qq','10001','message.reply',?,'dispatching',?)", (
                       attempt, "synthetic-recovery", env.store.ledger_encode("core_actions.body", {}), env.core.clock().isoformat()))
    assert not reminder_runner.claim(env.store, env.module, now + 2 * reminder_runner.LEASE + 2)
    assert reminders.listing(env.store, id=alarm["id"])["items"][0]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_low_affinity_direct_help_and_high_affinity_care(env):
    raw = incoming(851, text="我有点慌，可以帮我先理理吗？")
    actor = normalize(raw).actor
    doc = {"schema_version": 1, "id": actor.key, "display_name": "合成对象",
           "affinity": 25, "interactions": 4, "events": [], "locked": False}
    old = env.store.write("relationships", doc, None, "test", actor.key)
    original = env.model.generate
    async def direct(instructions, context, schema, *args, **kwargs):
        assert "role_tools" not in context and "continuation" in schema["properties"]
        result = await original(instructions, context, schema, *args, **kwargs)
        result["action"]["arguments"]["text"] = "我在。你想先从哪件事说起？"
        return result
    env.model.generate = direct
    await handle(env, raw)
    assert env.provider.sent[-1][1] == "我在。你想先从哪件事说起？"
    env.model.generate = original
    env.store.write("relationships", {**doc, "affinity": 83}, old["version"], "test", actor.key)
    alarm = await setup_reminder(env, 852, text="明天上午去报名", source="care")
    assert alarm["source"] == "care"
    current = env.store.read("relationships", actor.key)
    env.store.write("relationships", {**current["data"], "affinity": 25}, current["version"], "test", actor.key)
    before = len(env.model.calls)
    stamp = env.core.clock() + timedelta(minutes=2)
    env.core.clock = lambda: stamp
    await reminder_runner.tick(env.core, env.module)
    assert len(env.model.calls) == before
    assert reminders.listing(env.store, id=alarm["id"])["items"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_observation_only_receiver_reads_new_window_without_reply_or_model(env):
    from tests_postgres.test_qq_native_receiver import Worker, bound, receiver
    scope = ReplyScope("qq", "10001", "group", "10003")
    permissions = ReplyAuthorizations(env.store)
    permissions.update(scope, enabled=False, expected_version=permissions.version(scope), actor="test")
    with env.store.db() as db:
        group_modes.save(env.store, scope, None, True, False, env.core.clock().timestamp()-1, db)
    worker = Worker(2)
    for message in worker.messages:
        message["elements"][0]["textElement"]["content"] = "我在听"
    source = receiver(env, worker)
    source.enroll("group", "10003", bound(1))
    assert source.poll("group", "10003")["status"] == "staged"
    assert source.poll("group", "10003")["complete"]
    assert await env.module.runtime_poll() == []
    assert expression_learning.context(env.store)
    assert not env.provider.sent and not env.model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reschedule", "cancel", "complete"])
async def test_original_speaker_can_edit_reminder_in_conversation(env, operation):
    alarm = await setup_reminder(env, 860)
    when = env.core.clock() + timedelta(days=2)
    text = {"reschedule": "改到后天提醒我", "cancel": "取消提醒吧", "complete": "我已经交了材料"}[operation]
    original = env.model.generate
    async def update(prompt, context, schema, *args, **opts):
        value = await original(prompt, context, schema, *args, **opts)
        assert context["role_activity"]["waiting"][0]["id"] == alarm["id"]
        value["continuation"] = {"matter":alarm["id"],"next":{"reschedule":"alarm","cancel":"stop","complete":"end"}[operation],
                                 "wake_at":when.isoformat() if operation=="reschedule" else None}
        return value
    env.model.generate = update
    await handle(env, incoming(861, text=text))
    after = reminders.listing(env.store, id=alarm["id"])["items"][0]
    assert after["version"] != alarm["version"]
    assert after["status"] == {"reschedule": "pending", "cancel": "cancelled", "complete": "completed"}[operation]
    if operation == "reschedule":
        assert after["due_at"] == when.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [False, True])
async def test_due_uses_the_qq_native_journal_around_submission(env, lost):
    from tests_postgres.test_qq_native_sender import install
    alarm = await setup_reminder(env, 870)
    worker, sender = install(env)
    env.provider.send_reply_action = sender.send_reply_action
    worker.lose_receipt = lost
    when = env.core.clock() + timedelta(minutes=2)
    env.core.clock = lambda: when
    async def decide(*args, **opts):
        return bound_turn("记得带报名材料。")
    env.model.generate = decide
    await reminder_runner.tick(env.core, env.module)
    assert reminders.listing(env.store, id=alarm["id"])["items"][0]["status"] == ("uncertain" if lost else "sent")
    if lost:
        assert env.module.tainted
        env.core.stop("synthetic-restart")
        await env.module.close()
        env.core.start()
        await env.module.runtime_poll()
    await reminder_runner.tick(env.core, env.module)
    assert worker.submits == 1


@pytest.mark.asyncio
async def test_pre_submit_quota_denial_waits_then_checks_again(env):
    alarm = await setup_reminder(env, 880)
    scope = ReplyScope("qq", "10001", "private", "10002")
    policies = env.module.participation.policies
    policies.update_object(scope, {"private_daily_replies": 1}, policies.effective(scope)["version"], "test")
    clock = [env.core.clock() + timedelta(minutes=2)]
    env.core.clock = env.module.participation.clock = lambda: clock[0]
    reviews = []
    async def decide(prompt, context, *args, **opts):
        reviews.append(context["clue"])
        return bound_turn("记得拿材料。")
    env.model.generate = decide
    await reminder_runner.tick(env.core, env.module)
    assert reminders.listing(env.store, id=alarm["id"])["items"][0]["status"] == "pending"
    await reminder_runner.tick(env.core, env.module)
    assert len(reviews) == 1 and len(env.provider.sent) == 1
    clock[0] += timedelta(days=1)
    await reminder_runner.tick(env.core, env.module)
    assert len(reviews) == 2 and len(env.provider.sent) == 2
    assert reminders.listing(env.store, id=alarm["id"])["items"][0]["status"] == "sent"
