"""Generic decisions through desktop ingress and wake, with synthetic peers and receipts."""

import asyncio
from datetime import timedelta

import pytest

from companion_core import role_matters
from tests_postgres.test_desktop_reply_quota import result
from tests_postgres.test_qq_module import env as env
from tests_postgres.test_windows_role_matters import setup_pair
from tests_postgres.test_windows_role_tools import manager

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(("seed","inbox","spoken","external"), [
    ("咱们都喜欢编面包星球的故事","可颂要给烤箱放个假","面包星球今天换吐司来值班。",True),
    ("你可以找我聊聊今天过得怎样","我觉得今天你帮倒忙了","今天有些沮丧，想聊两句。",True),
    ("有需要再联系","时间已经弄清，刚才是个玩笑","知道了，明晚见。",False),
])
async def test_same_chat_protocol_supports_social_choices_and_natural_end(env,seed,inbox,spoken,external):
    from companion_core.person_baseline import SELECTION
    schedule,receive=await setup_pair(env)
    old=env.model.generate
    compose=[]
    sender_private="发起人未共享的身份标记"
    async def model(prompt,context,schema,*args,**opts):
        if "provided" in context:
            assert seed in str(context["conversation"]) and sender_private not in str(context)
            compose.append(context["audience"])
            return {"send":True,"text":spoken}
        value=await old(prompt,context,schema,*args,**opts)
        value["action"]=None
        if inbox in str(context):
            if external:
                contact=next(c for c in context["role_activity"]["contacts"] if c.get("configured_relationship"))
                value["action"]={"capability":"message.reply","arguments":{"targets":[contact["id"]]}}
            else:
                value["action"]={"capability":"message.reply","arguments":{"text":spoken}}
            value["continuation"]=control("end")
        return value
    env.model.generate=model
    await receive(4750,"10004",seed)
    with env.store.db() as db:
        actor=db.execute("SELECT actor FROM matter_contacts").fetchone()[0]
        env.store.set_state(SELECTION,{"anchor":actor,"selected":[actor]},db)
    await receive(4751,"10002",inbox+sender_private)
    await schedule._step("qq")
    assert [(s.target_id,t) for s,t in env.provider.sent]==[("10004" if external else "10002",spoken)]
    assert len(compose)==int(external)


def control(next, matter=None, wake=None):
    return {"matter": matter, "next": next, "wake_at": wake.isoformat() if wake else None}


async def test_destination_is_chosen_on_ingress_but_never_inherits_source_draft(env, record_property):
    import json

    schedule, receive = await setup_pair(env)
    original = env.model.generate
    private = "在霞影巷77号见代号砂砾"
    calls = []

    async def model(prompt, context, schema, *args, **opts):
        if "provided" in context:
            calls.append("compose")
            assert private not in str(context) and "sk-synthetic0000123" not in str(context)
            assert "代号砂砾" not in prompt and "intent" not in context
            assert "暗号小磁铁" not in str(context) and "提示专用名" not in str(context)
            assert "青色纸风车" in str(context["conversation"])
            record_property("recipient_10004_before_review", json.dumps(context, ensure_ascii=False))
            return {"send": True, "text": "你的青色纸风车做好了吗？"}
        value = await original(prompt, context, schema, *args, **opts)
        value["action"] = None
        assert "tool_call" not in schema["properties"]
        if private in str(context):
            targets = [t["id"] for t in context["role_activity"]["contacts"] if not t["current"]]
            assert len(targets) == 1
            value.update(
                action={"capability": "message.reply", "arguments": {"targets": targets}},
                continuation={
                    **control("alarm", wake=env.core.clock() + timedelta(hours=1)),
                    "note": "暗号小磁铁",
                },
            )
            calls.append("choose")
            from jsonschema import Draft202012Validator
            protocol = Draft202012Validator(schema)
            assert protocol.is_valid(value)
            mixed = {"capability":"message.reply","arguments":{"targets":targets,"text":private}}
            assert not protocol.is_valid({**value,"action":mixed})
            unknown = {"capability":"message.reply","arguments":{"targets":["an-invented-recipient"]}}
            assert not protocol.is_valid({**value,"action":unknown})
            assert not protocol.is_valid({**value,"tool_call":{"name":"matter.open","arguments":{"quote":private}}})
        return value

    env.model.generate = model
    card = env.store.read("persona")
    setting = {
        "target": "person",
        "owner_name": "提示专用名",
        "addresses": ["暗号小磁铁"],
        "level": "熟悉",
        "description": "合成专属关系",
        "source": {
            "type": "user_instruction",
            "reference": "manual-person-baseline",
            "recorded_at": env.core.clock().isoformat(),
        },
    }
    env.store.write("persona", {**card["data"], "relationship_baseline": setting}, card["version"], "fixture")
    await receive(4100, "10004", "我的青色纸风车做好一半了")
    await receive(4101, "10002", private + "。令牌=sk-synthetic0000123")
    assert calls == ["choose", "compose"]
    assert [(s.target_id, t) for s, t in env.provider.sent] == [("10004", "你的青色纸风车做好了吗？")]
    await asyncio.gather(schedule._step("qq"), schedule._step("qq"))
    assert len(env.provider.sent) == 1
    with env.store.db() as db:
        assert db.execute("SELECT count(*) FROM core_actions WHERE status='succeeded'").fetchone()[0] == 1


async def test_idle_alarm_backs_off_and_new_inbound_can_continue(env):
    schedule, receive = await setup_pair(env)
    original, now = env.model.generate, [env.core.clock()]
    env.core.clock = env.module.participation.clock = lambda: now[0]
    wakes = []

    async def model(prompt, context, schema, *args, **opts):
        if "contacts" in context:
            wakes.append(now[0])
            return {"targets": [], "continuation": control("alarm", wake=now[0] + timedelta(seconds=1))}
        value = await original(prompt, context, schema, *args, **opts)
        value["action"] = None
        activity = context["role_activity"]
        identifier = activity["waiting"][0]["id"] if activity["waiting"] else None
        value["continuation"] = control("alarm", identifier, now[0] + timedelta(seconds=1))
        return value

    env.model.generate = model
    api = manager(env.store, lambda: now[0].timestamp())
    await receive(4201, "10002", "可以晚点接着聊")

    def get():
        return result(api, "matters.list", {"after": None})["items"][0]

    identifier = get()["id"]
    for delay in (1, 30, 120, 600, 3600):
        row = get()
        from datetime import datetime

        due = datetime.fromisoformat(row["wake_at"])
        assert (due - now[0]).total_seconds() == delay
        now[0] = due
        await asyncio.gather(schedule._step("qq"), schedule._step("qq"))
    assert len(wakes) == 5 and get()["wake_at"] is None and get()["status"] == "waiting"
    now[0] += timedelta(days=1)
    await schedule._step("qq")
    assert len(wakes) == 5
    await receive(4202, "10002", "有新进展，花样做好了")
    assert get()["id"] == identifier and datetime.fromisoformat(get()["wake_at"]) == now[0] + timedelta(
        seconds=1
    )
    assert not env.provider.sent


async def test_distinct_alarms_at_the_same_instant_survive_restart_and_wake_independently(env):
    schedule, receive = await setup_pair(env)
    old = env.model.generate
    now = env.core.clock()
    when = now + timedelta(seconds=90)
    notes = []

    async def decide(prompt, context, schema, *args, **opts):
        if "contacts" in context:
            notes.append(context["own_note"])
            return {"targets": [], "continuation": control("end")}
        value = await old(prompt, context, schema, *args, **opts)
        value["action"] = None
        note = (
            "先看折纸" if "折纸" in context["event"]["text"] + str(context.get("backlog", {})) else "再看风车"
        )
        value["continuation"] = {**control("alarm", wake=when), "note": note}
        return value

    env.model.generate = decide
    await receive(4250, "10002", "折纸这件事")
    await receive(4251, "10002", "还有风车这件事")
    api = manager(env.store, lambda: env.core.clock().timestamp())
    rows = result(api, "matters.list", {"after": None})["items"]
    assert len(rows) == 2 and all(r["wake_at"] == when.isoformat() for r in rows)
    assert "先看折纸" not in str(rows)
    env.core.stop()
    await env.module.close()
    env.core.clock = lambda: when
    env.core.start()
    for _ in range(3):
        await schedule._step("qq")
    assert sorted(notes) == sorted(["先看折纸", "再看风车"]) and not env.provider.sent


@pytest.mark.parametrize("prior", [None, "succeeded", "dispatching"])
async def test_older_step_uses_generic_composer_and_original_dedupe_key(env, prior):
    schedule, receive = await setup_pair(env)
    old = env.model.generate

    async def quiet(prompt, context, schema, *args, **opts):
        if "provided" in context:
            assert "intent" not in context and "涂鸦暗号" not in str(context)
            return {"send": True, "text": "你那边方便聊天吗？"}
        value = await old(prompt, context, schema, *args, **opts)
        value["action"] = None
        return value

    env.model.generate = quiet
    await receive(4300, "10002", "涂鸦暗号")
    await receive(4301, "10004", "我现在有空")
    with env.store.db() as db:
        contacts = db.execute("SELECT * FROM matter_contacts ORDER BY occurred,id").fetchall()
        from companion_core import events
        from companion_core.contracts import Event

        records = [Event.from_dict(events.get(env.store, c["anchor"])["event"]) for c in contacts]
        src = next(e for e in records if e.text == "涂鸦暗号")
        dest = next(c for c in contacts if c["anchor"] != src.id)
        key = role_matters.create(
            env.store, src, events.get(env.store, src.id)["version"], env.core.clock().timestamp(), db
        )
        row = db.execute("SELECT * FROM role_matters WHERE id=?", (key,)).fetchone()
        body = role_matters.unpack(env.store, row)
        body.update(
            steps=[
                {
                    "id": "synthetic-old-dedupe",
                    "intent": "ask",
                    "target": dest["id"],
                    "basis": dest["permission"],
                }
            ],
            next="finish",
        )
        db.execute(
            "UPDATE role_matters SET body=? WHERE id=?", (role_matters.pack(env.store, key, body), key)
        )
        if prior:
            db.execute(
                "INSERT INTO core_actions(id,dedupe,generation,platform,account,capability,body,status,created) "
                "VALUES('previous-action','synthetic-old-dedupe','previous','qq','10001','message.reply',?,?,?)",
                (env.store.ledger_encode("core_actions.body", {}), prior, env.core.clock().isoformat()),
            )
    env.core.stop()
    await env.module.close()
    env.core.start()
    await schedule._step("qq")
    await schedule._step("qq")
    assert len(env.provider.sent) == int(prior is None)
    if not prior:
        assert env.provider.sent[0][0].target_id == "10004"
    with env.store.db() as db:
        status = "uncertain" if prior == "dispatching" else "succeeded"
        assert (
            db.execute("SELECT status FROM core_actions WHERE dedupe='synthetic-old-dedupe'").fetchone()[0]
            == status
        )
        assert db.execute("SELECT status FROM role_matters WHERE id=?", (key,)).fetchone()[0] == (
            "uncertain" if prior == "dispatching" else "completed"
        )


@pytest.mark.parametrize("change", ["stop", "revoke"])
async def test_scope_change_while_target_composes_prevents_submission(env, change):
    from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope

    schedule, receive = await setup_pair(env)
    api = manager(env.store, lambda: env.core.clock().timestamp())
    old = env.model.generate
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(prompt, context, schema, *args, **opts):
        if "provided" in context:
            entered.set()
            await release.wait()
            return {"send": True, "text": "合成普通消息"}
        value = await old(prompt, context, schema, *args, **opts)
        value["action"] = None
        if "现在联系" in str(context):
            choices = context["role_activity"]["contacts"]
            value["action"] = {
                "capability": "message.reply",
                "arguments": {"targets": [next(c["id"] for c in choices if not c["current"])]},
            }
        return value

    env.model.generate = model
    await receive(4400, "10004", "你好")
    await receive(4401, "10002", "现在联系", intake_only=True)
    first = asyncio.create_task(env.core.role.wake(env.module, {"qq": env.core}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await env.core.role.wake(env.module, {"qq": env.core})
        if change == "stop":
            row = result(api, "matters.list", {"after": None})["items"][0]
            result(api, "matters.stop", {"id": row["id"], "expected": row["version"]})
        else:
            auth = ReplyAuthorizations(env.store)
            scope = ReplyScope("qq", "10001", "private", "10004")
            auth.update(scope, enabled=False, expected_version=auth.version(scope), actor="fixture")
    finally:
        release.set()
        await first
    assert not env.provider.sent
    assert result(api, "matters.list", {"after": None})["items"][0]["status"] == "stopped"


async def test_two_authorized_group_members_are_one_delivery_but_two_private_chats_are_two(env):
    from tests.test_qq_native_normalization import normalize
    from tests_postgres.test_qq_module import incoming
    from tests_postgres.test_windows_group_growth import group_settings, relationship

    group_settings(env, True, True)
    schedule, receive = await setup_pair(env, ("10004", "10006"))
    old = env.model.generate
    views = []

    async def choose(prompt, context, schema, *args, **opts):
        if "provided" in context:
            if context["audience"] == "group":
                views.append(context["conversation"])
            return {"send": True, "text": "大家好"}
        value = await old(prompt, context, schema, *args, **opts)
        value["action"] = None
        if "给可见会话打招呼" in str(context):
            targets = [c for c in context["role_activity"]["contacts"] if not c["current"]]
            groups = [c for c in targets if c["audience"] == "group"]
            assert len(groups) == 2 and groups[0]["conversation"] == groups[1]["conversation"]
            value["action"] = {
                "capability": "message.reply",
                "arguments": {"targets": [c["id"] for c in targets]},
            }
        return value

    env.model.generate = choose
    for index, uid in enumerate(("10002", "10004")):
        raw = incoming(4500 + index, sender=uid, group=True)
        if uid == "10004":
            raw["senderUid"] = "u_peer_10004"
        event = normalize(raw)
        relationship(env, event.actor, 90)
        await receive(4500 + index, uid, "群成员" + uid + "发言", group=True)
        await receive(4510 + index, uid, "各自的合成私聊")
    await receive(4520, "10006", "给可见会话打招呼")
    for _ in range(4):
        await schedule._step("qq")
    assert sorted((s.kind, s.target_id) for s, _ in env.provider.sent) == [
        ("group", "10003"),
        ("private", "10002"),
        ("private", "10004"),
    ]
    assert len(views) == 1
    assert ("群成员10002发言" in str(views[0])) != ("群成员10004发言" in str(views[0]))
