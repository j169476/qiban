"""Bilibili public timer. The role selects one available tool in its own public context."""

from copy import copy

from bili_assistant.core_binding import CoreScheduler
from bili_assistant.workflow import BilibiliWorkflow
from companion_core.orchestration import Flow
from companion_core.public_workspace import PublicWorkspace
from companion_core.schema import obj


async def wake(core, adapter):
    run = core.guard()
    if run.get("reply_test") or run["read_only"] or core.remaining_model_calls() <= 1:
        return
    engine = copy(adapter.engine)
    engine.store = PublicWorkspace(core.store)
    flow = BilibiliWorkflow(CoreScheduler(engine, core))
    state = {"policy": core.store.read("policy")["data"], "inbox": []}
    flow.emotion_recovery(state)
    await flow.delivery_recovery(state)
    if await flow.prepare(state, include_inbox=False) in {Flow.STOP, Flow.FINALIZE}:
        return
    settings = state["settings"]
    tools = {"wait": "这次不活动"}
    if state["room"]:
        tools["browse"] = "查看已启用的视频源；结合当前视频决定观看、互动或离开"
    if settings.get("native_tools_enabled"):
        tools["native"] = "使用当前启用的资料工具"
    if hasattr(engine.adapter, "favorite_tools"):
        tools["favorite"] = "在当前账号授权范围管理收藏"
    tools.update(video_gap="继续已开始的视频", goal_learning="继续已有学习事项")
    if settings.get("daily_reflection"):
        tools["reflect"] = "整理公开反思"
    if settings.get("memory_maintenance"):
        tools["maintain"] = "整理已许可的资料"
    from companion_core import baseline

    bundle = baseline.load(engine.store)
    persona = bundle.context if bundle else {"persona": engine.store.read("persona")["data"]}
    material = {**persona, "available_tools": tools, "now": core.clock().isoformat()}
    if bundle:
        engine.store.baseline_guards.append(bundle.check)
    guard = engine.store.before_model(material)
    choice = await core.model(
        "这是公开平台的定时唤醒。按人物卡和当前可用工具自主选择，也可以等待下一次唤醒。",
        material,
        obj({"tool": {"enum": list(tools)}}),
        guard=guard,
        reserve_for_replies=1,
    )
    guard()
    if choice["tool"] == "browse":
        if await flow.explore(state) not in {Flow.FINALIZE, Flow.STOP}:
            await flow.decide(state)
    elif choice["tool"] == "favorite":
        from bili_assistant.favorite_workflow import step

        await step(CoreScheduler(adapter.engine, core))
    elif choice["tool"] != "wait":
        methods = {
            "native": flow.native_tools,
            "video_gap": flow.video_gap,
            "goal_learning": flow.goal_learning,
            "reflect": flow.reflect,
            "maintain": flow.maintain,
        }
        await methods[choice["tool"]](state)
