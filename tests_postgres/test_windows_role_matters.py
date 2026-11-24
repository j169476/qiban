"""Windows role runtime, synthesized contacts & model. No network or installed accounts."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from companion_core import role_matters
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from qiban.desktop_scheduler import DesktopScheduler
from tests.test_qq_native_normalization import IDENTITY
from tests_postgres.test_desktop_reply_quota import denied, result
from tests_postgres.test_qq_module import env as env
from tests_postgres.test_qq_module import incoming
from tests_postgres.test_windows_role_tools import manager

pytestmark = pytest.mark.asyncio


async def setup_pair(env, others=("10004",)):
    opts = env.module.options
    opts["private_targets"] = ["10002", *others]
    env.provider.probe = lambda: dict(
        account="10001", account_label="合成", private_targets=opts["private_targets"], groups=["10003"]
    )
    env.module.probe()
    for uid in others:
        scope = ReplyScope("qq", "10001", "private", uid)
        ReplyAuthorizations(env.store).update(scope, enabled=True, expected_version=None, actor="synthetic")
    directory = {**IDENTITY, "friends": {**IDENTITY["friends"], **{uid: "u_peer_" + uid for uid in others}}}
    schedule = DesktopScheduler(SimpleNamespace(store=env.store, cores={"qq": env.core}))

    async def receive(n, who, text, *, intake_only=False, group=False):
        raw = incoming(n, text=text, sender=who, group=group,mention=group)
        raw.update(
            peerUid="10003" if group else directory["friends"][who],
            senderUid=directory["friends"][who],
            msgTime=str(int(env.core.clock().timestamp())),
        )
        env.module.accept_native(raw, identity=directory)
        if intake_only:
            for event in await env.module.runtime_poll():
                await env.core.handle(event)
        else:
            await schedule._step("qq")

    return schedule, receive


@pytest.mark.parametrize("outcome", ["sent", "uncertain", "stopped"])
async def test_two_people_resume_and_keep_separate_sources(env, outcome, record_property):
    import json
    schedule, receive = await setup_pair(env)
    calls = []
    original = env.model.generate
    api = manager(env.store, lambda: env.core.clock().timestamp())
    first = "周六公开桌游是六点半"
    second = "周六公开桌游刚改成七点"
    private = "我家在合成小巷9号"
    private_summary = private + "，原会话摘要标记"
    private_memory = private + "，原会话记忆标记"
    organizer = "我是桌游组织者"

    def meeting(text):
        return dict(
            quote=text,
            value="桌游",
            category="ordinary",
            level="social",
            subject="none",
            evidence="general",
            until=None,
        )

    async def model(prompt, context, schema, *args, **kw):
        calls.append((prompt, context))
        if "current" in context:
            assert "sk-example0123456789" not in str(context)
            text = context["current"]
            return {"facts": [meeting(s) for s in [first, second, organizer] if s in text]}
        if "contacts" in context:
            pytest.fail("direct inbox selection redundantly invoked a timer decision")
        if "provided" in context:
            assert "sk-example0123456789" not in str(context)
            texts = [r["text"] for r in context["provided"]]
            if second in texts:
                # The original speaker can still read their own private turn on the return leg.
                assert private in str(context["conversation"])
                record_property("recipient_10002",json.dumps(context,ensure_ascii=False))
                return dict(send=True, text="场次刚改到七点。")
            assert private not in str(context)
            assert private_summary not in str(context) and private_memory not in str(context)
            assert first in texts
            record_property("recipient_10004",json.dumps(context,ensure_ascii=False))
            return dict(send=True, text="周六桌游几点开始？")
        value = await original(prompt, context, schema, *args, **kw)
        value["action"] = None
        current = context["event"]["text"] + " ".join(
            u.get("text", "") for u in context.get("backlog", {}).get("units", []))
        activity = context["role_activity"]
        if first in current:
            value["memories"] = [private_memory]
            target = next(c["id"] for c in activity["contacts"] if not c["current"])
            value.update(action={"capability":"message.reply","arguments":{"targets":[target]}},
                         continuation={"matter":None,"next":"wait","wake_at":None})
        elif second in current:
            item = activity["waiting"][0]
            assert item["previous_message"] == "周六桌游几点开始？"
            value.update(action={"capability":"message.reply","arguments":{"targets":[item["origin"]]}},
                         continuation={"matter":item["id"],"next":"end","wake_at":None})
        return value

    env.model.generate = model
    # Write and review the public event fragment before the planner's send step.
    # A normal scheduler tick reviews one source fragment; outstanding records
    # remain private and may never enter the other person's context.
    await receive(1001, "10004", organizer)
    await receive(1002, "10002", first + "；" + private + "；令牌=sk-example0123456789", intake_only=True)
    from companion_core import conversations, events
    from companion_core.contracts import Event
    from companion_core.memory_maintenance import save_compression
    with env.store.db() as db:
        anchors=[r[0] for r in db.execute("SELECT anchor FROM matter_contacts")]
    event=next(ev for key in anchors if private in (ev:=Event.from_dict(events.get(env.store,key)["event"])).text)
    scope=env.core.conversation_scope(event)
    tid=conversations.thread_id(scope["account"],scope["channel"],scope["key"])
    turns=conversations.records(env.store,scope["account"],tid)
    spans=[{"id":r["data"]["id"],"version":r["version"],"start":0,
            "end":len(r["data"]["content"]),"total":len(r["data"]["content"])} for r in turns]
    summary=save_compression(env.core,{"account":scope["account"],"thread_id":tid,"turns":turns,
                                       "spans":spans,"previous":None,"chain":[],"summary_limit":300},
                             {"summary":private_summary})
    env.core.governed_context(event).classify_written("conversation_summaries",summary,sources=["event/"+event.id])
    assert result(api, "matters.list", {"after": None})["items"], [
        (list(c), c.get("event", {}).get("text"), c.get("role_tools"), c.get("backlog")) for p, c in calls
    ]
    for _ in range(24):
        from companion_core import privacy_review

        await privacy_review.tick(env.core)
    if outcome == "uncertain":
        env.provider.unknown = True
    if outcome == "stopped":
        current = result(api, "matters.list", {"after": None})["items"][0]
        stop = result(api, "matters.stop", {"id": current["id"], "expected": current["version"]})
        assert stop["status"] == "stopped"
        await schedule._step("qq")
        assert not env.provider.sent
        return
    await schedule._step("qq")
    row = result(api, "matters.list", {"after": None})["items"][0]
    if outcome == "uncertain":
        assert row["status"] == row["delivery"] == "uncertain"
        from companion_core.errors import AssistantError

        with pytest.raises(AssistantError) as stopped:
            await schedule._step("qq")
        assert stopped.value.code == "qq_send_unconfirmed"
        env.core.stop()
        await env.module.close()
        env.provider.unknown = False
        env.core.start()
        await schedule._step("qq")
        assert len(env.provider.sent) == 1
        return
    assert (row["status"], row["delivery"]) == ("waiting", "sent")
    stamp = env.core.clock() + timedelta(days=1)
    env.core.stop()
    await env.module.close()
    env.core.clock = env.module.participation.clock = lambda: stamp
    env.core.start()
    await env.module.runtime_poll()
    await receive(1003, "10004", second, intake_only=True)
    from companion_core import privacy_review

    for _ in range(24):
        await privacy_review.tick(env.core)
    await schedule._step("qq")
    assert [(sc.target_id, t) for sc, t in env.provider.sent] == [
        ("10004", "周六桌游几点开始？"),
        ("10002", "场次刚改到七点。"),
    ], (
        result(api, "matters.list", {"after": None}),
        [
            (
                list(c),
                c.get("intent"),
                c.get("current"),
                c.get("provided"),
                c.get("role_tools", {}).get("waiting"),
            )
            for p, c in calls
        ],
    )
    await receive(1003, "10004", second)
    assert len(env.provider.sent) == 2
    assert result(api, "matters.list", {"after": None})["items"][0]["status"] == "completed"
    with env.store.db() as db:
        body = role_matters.unpack(env.store, db.execute("SELECT * FROM role_matters").fetchone())
        assert len(body["sources"]) == 2 and body["sources"][0]["actor"] != body["sources"][1]["actor"]
        assert body["sources"][0]["at"] < body["sources"][1]["at"]


async def test_role_chooses_two_contacts_and_later_ends_on_its_own_timer(env):
    from companion_core import privacy_review

    schedule, receive = await setup_pair(env, ("10004", "10006"))
    text = "社团公开活动几点开始"
    now = env.core.clock()
    old = env.model.generate
    plans = []

    async def model(prompt, context, schema, *args, **kw):
        if "current" in context:
            return {
                "facts": [
                    {
                        "quote": text,
                        "value": "活动",
                        "category": "ordinary",
                        "level": "social",
                        "subject": "none",
                        "evidence": "general",
                        "until": None,
                    }
                ]
                if text in context["current"]
                else []
            }
        if "contacts" in context:
            plans.append(context)
            return {"targets": [],"continuation":{"matter":None,"next":"end","wake_at":None}}
        if "provided" in context:
            return {"send": True, "text": "周末活动几点开始？"}
        value = await old(prompt, context, schema, *args, **kw)
        value["action"] = None
        if text in str(context.get("backlog", {})) or text in context["event"]["text"]:
            contacts = context["role_activity"]["contacts"]
            value["action"] = {"capability":"message.reply","arguments":{"targets":[c["id"] for c in contacts if not c["current"]]}}
            value["continuation"] = {"matter":None,"next":"alarm","wake_at":(now+timedelta(hours=2)).isoformat()}
        return value

    env.model.generate = model
    await receive(1201, "10004", "你好")
    await receive(1202, "10006", "你好")
    await receive(1203, "10002", text)
    for _ in range(15):
        await privacy_review.tick(env.core)
    for _ in range(3):
        await schedule._step("qq")
    assert sorted(sc.target_id for sc, _ in env.provider.sent) == ["10004", "10006"]
    assert not plans
    env.core.clock = lambda: now + timedelta(hours=3)
    await schedule._step("qq")
    api = manager(env.store, lambda: env.core.clock().timestamp())
    assert result(api, "matters.list", {"after": None})["items"][0]["status"] == "completed"
    assert len(plans) == 1 and len(env.provider.sent) == 2


async def test_admin_projection_and_pending_by_default(pg_store):
    from tests_postgres.matter_fixture import seed

    seed(pg_store)
    api = manager(pg_store, lambda: 1792922400)
    rows = result(api, "privacy.list", {"after": None})["items"]
    credential = next(r for r in rows if r["level"] == "credential")
    assert not credential["choices"] and "synthetic-fixture" not in str(rows)
    circle = next(r for r in rows if r["level"] == "circle")
    change = {"id": circle["id"], "expected": circle["version"], "level": "private"}
    assert result(api, "privacy.correct", change)["level"] == "private"
    assert denied(api, "privacy.correct", change) == "edit_conflict"
    assert (
        denied(api, "privacy.correct", {**change, "id": credential["id"], "expected": credential["version"]})
        == "privacy_scope"
    )
