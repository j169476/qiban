"""Synthetic conversations through the Windows intake, desktop scheduler, and durable ledgers."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from companion_core import expression_learning, expression_patterns, group_modes, reminders
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from qiban.desktop_scheduler import DesktopScheduler
from tests.test_qq_native_normalization import normalize
from tests_postgres.matter_fixture import bound_turn, legacy_alarm
from tests_postgres.test_qq_module import env as env
from tests_postgres.test_qq_module import handle, incoming

GROUP = ReplyScope("qq", "10001", "group", "10003")


def group_settings(env, listen, proactive):
    old = group_modes.settings(env.store, GROUP)
    with env.store.db() as db:
        return group_modes.save(env.store, GROUP, old["version"], listen, proactive, env.core.clock().timestamp() - 1, db)


def relationship(env, actor, affinity):
    doc = {"schema_version": 1, "id": actor.key, "display_name": "合成群成员", "affinity": affinity,
           "interactions": 4, "events": [], "locked": False}
    env.store.write("relationships", doc, None, "fixture", actor.key)


def desktop(env):
    return DesktopScheduler(SimpleNamespace(store=env.store, cores={"qq": env.core}))


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["care", "explicit", "spontaneous", "cooldown", "other_finished", "owner_finished", "revoke", "disable",
                                  "wrong_recipient", "listen_only", "low_affinity", "third_party"])
async def test_group_due_follows_its_original_member_and_permission(env, case):
    source = "explicit" if case == "explicit" else "care"
    msg = {"explicit": "请提醒我带报名材料", "third_party": "我朋友说：“我明天要去报到”"}.get(case, "我明天要去报到")
    raw = incoming(911, group=True, mention=case not in {"spontaneous", "cooldown"}, text=msg)
    actor = normalize(raw).actor
    relationship(env, actor, 25 if case in {"explicit", "low_affinity"} else 85)
    group_settings(env, case != "explicit", case not in {"explicit", "listen_only"})
    card = env.store.read("persona")
    now = env.core.clock()
    old = env.model.generate
    due_calls = []
    async def generate(prompt, context, schema, *args, **opts):
        if "clue" in context:
            due_calls.append(context)
            assert context["recipient"] == actor.key
            assert "蓝纸已经办好了" not in str(context["conversation"])
            done = "本人已经办好了" in str(context["conversation"])
            return bound_turn(None if done else "报到还顺利吗？",
                              recipient="wrong-member" if case == "wrong_recipient" else actor.key)
        answer = await old(prompt, context, schema, *args, **opts)
        if context.get("backlog") and msg in str(context["backlog"]):
            assert "role_tools" not in context and "tool_call" not in schema["properties"]
            if case in {"low_affinity","listen_only"}:
                assert "role_activity" not in context
        return answer
    env.model.generate = generate
    await handle(env, raw)
    if case not in {"low_affinity", "listen_only", "third_party"}:
        legacy_alarm(env.store,normalize(raw),now+timedelta(minutes=1 if case=="cooldown" else 20),msg,source)
    jobs = reminders.listing(env.store)["items"]
    if case in {"low_affinity", "listen_only", "third_party"}:
        # These incoming messages explicitly mention the role. Its ordinary response
        # remains permitted; listening alone cannot turn that into a later delivery.
        assert not jobs and len(env.provider.sent)==1 and env.provider.sent[0][0]==GROUP
        env.core.clock = lambda: now+timedelta(days=1)
        await desktop(env)._step("qq")
        assert len(env.provider.sent)==1
        return
    assert len(jobs) == 1 and jobs[0]["source"] == source
    if case == "revoke":
        auth = ReplyAuthorizations(env.store)
        auth.update(GROUP, enabled=False, expected_version=auth.version(GROUP), actor="test")
    elif case == "disable":
        group_settings(env, True, False)
        group_settings(env, True, True)
    elif case in {"other_finished", "owner_finished"}:
        other = case == "other_finished"
        await handle(env, incoming(912, group=True, mention=True, sender="10004" if other else "10002",
                                   text="蓝纸已经办好了" if other else "本人已经办好了"))
    before = len(env.provider.sent)
    if case == "cooldown":
        env.core.clock = env.module.participation.clock = lambda: now + timedelta(minutes=2)
        await desktop(env)._step("qq")
        assert not due_calls and reminders.listing(env.store)["items"][0]["status"] == "pending"
    stamp = now + timedelta(minutes=21)
    env.core.clock = env.module.participation.clock = lambda: stamp
    await desktop(env)._step("qq")
    current = reminders.listing(env.store, id=jobs[0]["id"])["items"][0]
    if case in {"revoke", "disable"}:
        assert not due_calls and current["status"] == "cancelled"
    elif case == "wrong_recipient":
        assert due_calls and current["status"] == "cancelled"
    elif case == "owner_finished":
        assert due_calls and current["status"] == "completed"
    else:
        assert due_calls and current["status"] == "sent"
        assert env.provider.sent[-1] == (GROUP, "给 10002：报到还顺利吗？")
    assert len(env.provider.sent) == before + int(current["status"] == "sent")
    assert env.store.read("persona") == card
    assert env.store.read("relationships", actor.key)["data"]["affinity"] == (25 if case == "explicit" else 85)


@pytest.mark.asyncio
async def test_two_group_exchanges_teach_a_new_contextual_habit_used_in_another_conversation(env):
    phrases = [["合成暗号青柠，我的排练快截止了", "你已经排完开头了。只剩哪一段？", "先练结尾", "好，就做这一小步"],
               ["合成暗号纸舟，作品快交了", "轮廓已经出来了。先上底色还是画边线？", "先上底色", "这样一下就知道该做什么了"]]
    habit = "对方赶时间时，先点出已经做完的一小步，再用短问让对方选下一步，选好就收住。"
    signal = {"situation": "time_pressure", "moves": ["指出一个具体进展", "给出两个具体选项", "用短句收尾"],
              "tone": "克制", "outcome": "对方采纳"}
    group_settings(env, True, False)
    auth = ReplyAuthorizations(env.store)
    auth.update(GROUP, enabled=False, expected_version=auth.version(GROUP), actor="test")
    fixed_card = env.store.read("persona")
    now = [env.core.clock()]
    env.core.clock = env.module.participation.clock = lambda: now[0]
    original = env.model.generate
    learned, summaries, applied = [], [], []
    async def generate(prompt, context, schema, *args, **opts):
        if "turns" in context:
            learned.append(context["turns"])
            return {"cue": None, "expression_signal": signal}
        if "observations" in context:
            summaries.append(context)
            assert set(context) == {"situation", "observations"}
            assert len(context["observations"]) == 2
            assert "合成暗号" not in str(context) and "1000" not in str(context)
            return {"habit": habit}
        answer = await original(prompt, context, schema, *args, **opts)
        if "event" in context:
            assert "合成暗号" not in str(context)
            shared = context["shared_expression"]
            applied.append(list(shared))
            answer["action"]["arguments"]["text"] = ("开头已经有了。先写标题，还是先补结尾？"
                                                       if habit in shared else "想让我帮你看看吗？")
        return answer
    env.model.generate = generate
    async def accept(number, text, **opts):
        return await handle(env, {**incoming(number, text=text, **opts), "msgTime": str(int(now[0].timestamp()))})
    probe = "报告快截止了，开头写好了，后面还很乱"
    await accept(930, probe)
    assert not applied[-1] and env.provider.sent[-1][1] == "想让我帮你看看吗？"
    schedule = desktop(env)
    for index, lines in enumerate(phrases):
        now[0] += timedelta(hours=index, seconds=2)
        for n, line in enumerate(lines):
            await accept(940 + index * 10 + n, line, group=True, sender="10004" if n % 2 else "10002")
        await schedule._step("qq")
        assert len(learned) == index + 1
        assert len(summaries) == index
        assert len(env.provider.sent) == 1
    assert habit not in [v[0] for v in expression_learning.HABITS.values()]
    state = env.store.state(expression_patterns.KEY)
    assert state["time_pressure"]["habit"] == habit and "合成暗号" not in str(state)
    await accept(971, probe)
    assert applied[-1] == [habit] and env.provider.sent[-1][1] == "开头已经有了。先写标题，还是先补结尾？"
    await accept(972, "给我讲讲月相")
    assert habit not in applied[-1]
    assert env.store.read("persona") == fixed_card
