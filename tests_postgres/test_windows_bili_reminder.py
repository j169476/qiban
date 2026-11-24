"""Drive a delayed Bilibili DM through the desktop connector and its ordinary action ledger."""
from datetime import timedelta

import pytest

from bili_assistant.engine import Engine
from companion_core import reminders
from companion_core.reminder_runner import tick
from companion_core.runtime import Companion
from qiban.desktop_bili_connector import DesktopBiliConnector
from tests_postgres import test_wechat_role
from tests_postgres.matter_fixture import bound_turn, legacy_alarm
from tests_postgres.test_bili_governance import NOW, Model, Platform, patch
from tests_postgres.test_bili_governance import pg_profile as pg_profile
from tests_postgres.test_qq_module import env as env
from tests_postgres.test_qq_module import handle, incoming
from tests_postgres.test_runtime import Host

wechat = test_wechat_role.env


@pytest.mark.asyncio
async def test_generic_qq_turn_reaches_only_the_supported_bili_private_recipient(env):
    from companion_core import role_matters
    from tests_postgres.test_windows_role_matters import setup_pair
    pg_store=env.store
    patch(pg_store,"manifest",account_uid="123")
    patch(pg_store,"policy",min_action_interval_seconds=0)
    patch(pg_store,"companion",media_fallback="off",search_expansion=False,native_tools_enabled=False)
    host,platform,model=Host(),Platform(),Model()
    platform.incoming=[platform.incoming[0],platform.incoming[-1]]
    adapter=DesktopBiliConnector(Engine(pg_store,host,platform,lambda:NOW))
    bili=Companion(pg_store,model,host,[adapter],clock=lambda:NOW,run_key="core-run:bilibili")
    scheduler,receive=await setup_pair(env)
    cores=scheduler.runtime.cores
    cores["bilibili"]=bili
    env.core.role.cores=bili.role.cores=cores
    old_bili=model.generate
    async def bili_choice(prompt,context,schema,*args,**opts):
        if "provided" in context:
            assert "只在QQ说的细节" not in str(context)
            return {"send":True,"text":"普通会话问候"}
        value=await old_bili(prompt,context,schema,*args,**opts)
        value["action"]=None
        return value
    model.generate=bili_choice
    old_qq=env.model.generate
    async def qq_choice(prompt,context,schema,*args,**opts):
        value=await old_qq(prompt,context,schema,*args,**opts)
        choices=[c for c in context["role_activity"]["contacts"] if c["platform"]=="bilibili"]
        assert len(choices)==1 and choices[0]["audience"]=="private"
        value["action"]={"capability":"message.reply","arguments":{"targets":[choices[0]["id"]]}}
        return value
    env.model.generate=qq_choice
    bili.start()
    try:
        batch=await bili.poll()
        for event in batch:
            await bili.handle(event)
        assert any(ev.visibility=="public" for ev in batch)
        assert all(pair[0].visibility=="private" for pair in role_matters.candidates(cores,pg_store)[0].values())
        await receive(4700,"10002","只在QQ说的细节")
        assert [(kind,p["text"]) for kind,p in platform.sent]==[("dm_reply","普通会话问候")]
        assert not env.provider.sent
    finally:
        bili.stop()


@pytest.mark.asyncio
async def test_public_timer_selects_wait_without_reading_inbox_or_sending(pg_store):
    patch(pg_store,"manifest",account_uid="123")
    platform,host,model=Platform(),Host(),Model()
    calls=[]
    async def choose(prompt,context,schema,*args,**kwargs):
        calls.append(context)
        assert "wait" in context["available_tools"]
        return {"tool":"wait"}
    async def unexpected(*args,**kwargs):
        pytest.fail("a public timer selected wait but fetched inbox/feed or submitted an action")
    model.generate=choose
    platform.feed=platform.inbox=platform.perform=unexpected
    adapter=DesktopBiliConnector(Engine(pg_store,host,platform,lambda:NOW))
    core=Companion(pg_store,model,host,[adapter],clock=lambda:NOW,run_key="core-run:bilibili")
    core.start()
    try:
        await core.role.react("public_timer",adapter=adapter)
        assert len(calls)==1 and not platform.sent
    finally:
        core.stop()


@pytest.mark.asyncio
async def test_desktop_bili_reply_and_later_reminder_after_restart(pg_store):
    now = [NOW]
    patch(pg_store, "manifest", account_uid="123")
    patch(pg_store, "policy", min_action_interval_seconds=0)
    patch(pg_store, "companion", memory_maintenance=False, daily_reflection=False, knowledge_review=False,
          media_fallback="off", search_expansion=False, native_tools_enabled=False)
    platform, model, host = Platform(), Model(), Host()
    platform.incoming = platform.incoming[:1]
    source = platform.incoming[0]
    source["text"] = "明天提醒我拿车票"
    original = model.generate
    async def generate(instructions, context, schema, *args, **kwargs):
        if "clue" in context:
            return bound_turn("出门前记得拿车票。")
        value = await original(instructions, context, schema, *args, **kwargs)
        if "event" in context:
            assert "role_tools" not in context and "continuation" in schema["properties"]
        return value
    model.generate = generate
    def runtime():
        adapter = DesktopBiliConnector(Engine(pg_store, host, platform, lambda: now[0]))
        core = Companion(pg_store, model, host, [adapter], clock=lambda: now[0])
        core.start()
        return core, adapter
    core, adapter = runtime()
    batch = await core.poll()
    assert (await core.handle(batch[0]))["action"]["status"] == "succeeded"
    legacy_alarm(pg_store,batch[0],NOW+timedelta(days=1),"拿车票")
    core.stop()
    now[0] += timedelta(days=1, seconds=5)
    core, adapter = runtime()
    try:
        await tick(core, adapter)
        with pg_store.db() as db:
            receipts = [pg_store.ledger_decode("core_actions.result", r[0]) for r in db.execute(
                "SELECT result FROM core_actions WHERE result IS NOT NULL")]
        assert reminders.listing(pg_store)["items"][0]["status"] == "sent", receipts
        await tick(core, adapter)
        assert [p["text"] for _, p in platform.sent] == ["私密回复", "出门前记得拿车票。"]
        assert all(p["_event"]["uid"] == source["uid"] for _, p in platform.sent)
    finally:
        core.stop()


@pytest.mark.asyncio
async def test_authorized_wechat_and_public_bili_teach_a_shared_pattern_for_qq(env, wechat):
    from companion_core import expression_learning, expression_patterns
    from tests_postgres.test_wechat_role import enqueue
    from tests_wechat.test_receiver import row

    # Each connector runs on its ordinary data path with a synthetic account.
    signal = {"situation": "hesitation", "moves": ["先反问一个短问题", "给出两个具体选项"],
              "tone": "温和", "outcome": "明确偏好"}
    original = wechat.model.generate
    async def respond(prompt, context, schema, *args, **opts):
        value = await original(prompt, context, schema, *args, **opts)
        assert "expression_signal" in schema["properties"] and "role_tools" not in context
        return {**value, "action": None, "expression_signal": signal}
    wechat.model.generate = respond
    wechat.core.start()
    enqueue(wechat, row(1, text=wechat.session.nonce + " 我拿不准时，希望先听两个选项。合成私事梅子"))
    event, = await wechat.adapter.collect_once()
    await wechat.core.handle(event)
    assert len(env.store.state(expression_patterns.KEY)["hesitation"]["samples"]) == 1 and not wechat.driver.sent
    wechat.core.stop()

    store = env.store
    patch(store, "manifest", account_uid="123")
    patch(store, "policy", min_action_interval_seconds=0)
    patch(store, "companion", memory_maintenance=False, daily_reflection=False, knowledge_review=False,
          media_fallback="off", search_expansion=False, native_tools_enabled=False)
    platform, model, host = Platform(), Model(), Host()
    now = env.core.clock()
    public = {**platform.incoming[-1], "received": now.isoformat(), "text": "犹豫的时候，给我两个选项更好。"}
    platform.incoming = [public]
    habit = "对方在两种做法间犹豫时，给出各自的第一步，问对方愿意先试哪一个。"
    async def public_reply(prompt, context, schema, *args, **opts):
        assert "合成私事" not in str(context)
        if "observations" in context:
            assert set(context) == {"observations", "situation"} and len(context["observations"]) == 2
            return {"habit": habit}
        assert "expression_signal" in schema["properties"]
        assert context["shared_expression"] == ([habit] if context["event"]["native_id"] != public["id"] else [])
        model.calls.append(context)
        return {"note":"","action":None,"memories":[],"identity_proposals":[],"expression_signal":signal}
    model.generate = public_reply
    adapter = DesktopBiliConnector(Engine(store, host, platform, lambda: now))
    bili = Companion(store, model, host, [adapter], clock=lambda: now, run_key="core-run:bilibili")
    bili.start()
    try:
        await bili.cycle()
        assert len(store.state(expression_patterns.KEY)["hesitation"]["samples"]) == 2
        await expression_patterns.synthesize(bili)
        assert expression_learning.context(store, "我拿不准怎么选") == [habit]
        platform.incoming = [{**public, "id": "reply-34", "uid": "34", "text": "我在犹豫要报哪个社团"}]
        await bili.cycle()
        assert len(model.calls) == 2
    finally:
        bili.stop()
    await handle(env, incoming(990, text="我拿不准要画水彩还是素描"))
    assert env.model.calls[-1]["shared_expression"] == [habit] and "合成私事" not in str(env.model.calls[-1])
