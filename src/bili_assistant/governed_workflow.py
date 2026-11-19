"""Isolate private inbox replies from autonomous public-platform work."""
from copy import copy

from companion_core import delivery, events
from companion_core.orchestration import Flow, Stage, Workflow
from companion_core.public_workspace import PublicWorkspace

from .core_binding import CoreScheduler
from .workflow import BilibiliWorkflow


def definition(original, core, *, receive=True):
    engine = copy(original)
    workspace = PublicWorkspace(core.store)
    engine.store = workspace
    flow = BilibiliWorkflow(CoreScheduler(engine, core))

    # Collector and recovery run against trusted ingress, which assigns the
    # actual event's scope. Their private text is never placed in public state.
    async def inbox(*args, **kwargs):
        if not receive:
            return []
        result = await original.inbox(*args, **kwargs)
        return [{key: value for key, value in item.items() if key != "relationship_context"}
                for item in result if item["kind"] != "dm_reply"]

    engine.inbox = inbox

    async def private_replies(state):
        run = core.guard()
        if run["read_only"]:
            return
        pending = events.pending(core.store, [["bilibili", original.account]], run["pending_since"],
                                 owner=run["owner"], now=core.clock().isoformat(),
                                 config_token=delivery.config_token(core.store))
        # A bounded turn batch leaves room for exploration and new user input.
        for item in [event for event in pending if event.visibility == "private"][:2]:
            if not core.remaining_model_calls():
                return Flow.STOP
            await core.handle(item)

    async def prepare(state):
        result = await flow.prepare(state)
        if result in (Flow.STOP, Flow.FINALIZE):
            return result
        state["fresh"] = [{key: value for key, value in item.items() if key != "relationship_context"}
                          for item in state["fresh"] if item["kind"] != "dm_reply"]
        if not receive and not state["fresh"]:
            return Flow.STOP
        if not receive and not state["guided_bvid"]:
            state["room"] = 0
        workspace.readable = workspace.repository.readable_resources(workspace.access, workspace.request)
        return result

    async def favorite_tools(state):
        if not state["fresh"]:
            from .favorite_workflow import step
            # Private favorite folders stay in this account-only lane. They do
            # not enter public exploration's model packet or shared memories.
            await step(CoreScheduler(original, core))

    stages = []
    for stage in flow.definition().stages:
        if not receive and stage.name in {'native_tools','video_gap','goal_learning','favorite_tools'}:
            continue
        if stage.name == "favorite_tools":
            stages.append(Stage("favorite_tools", favorite_tools, 75, requires_model=True))
            continue
        if stage.name == "selection_context":
            stages.append(Stage("private_replies", private_replies, 35, requires_model=True))
            stages.append(Stage("selection_context", prepare, 40, "prepare"))
        else:
            stages.append(stage)
    return Workflow("bilibili", original.account, tuple(stages))
