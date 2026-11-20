"""Authenticated desktop operations for the real reply context workbench."""
from companion_core import context_workspace as workspace
from companion_core.context_budget import pack
from companion_core.decision import DecisionService
from companion_core.errors import AssistantError
from companion_core.structured_output import strict_schema

from . import desktop_context_presets, desktop_context_sources

FIELDS = {
    **desktop_context_presets.FIELDS, **desktop_context_sources.FIELDS,
    "workspace.capabilities": set(), "context.workspace": set(),
    "context.preview_config": {"scene", "config"},
    "context.history": {"after"},
}


def operate(manager, request, op, args):
    from . import desktop_learning

    manager.authority.require(request, administrator=True)
    store = manager.store
    if op == "workspace.capabilities":
        result = {"operations": sorted(FIELDS.keys() | desktop_learning.FIELDS.keys()),
                  "context_routes": ["local", "qq", "wechat", "bili_private"],
                  "context_scope": "installation", "preview_source": "synthetic",
                  "trace_detail": "reply_trace.detail", "learning_index": "background_on_demand"}
    elif op.startswith("learning."):
        result = desktop_learning.operate(manager, request, op, args)
    elif op in desktop_context_sources.FIELDS:
        result = desktop_context_sources.operate(manager, request, op, args)
    elif op in desktop_context_presets.FIELDS:
        result = desktop_context_presets.operate(manager, request, op, args)
    elif op == "context.workspace":
        result = workspace.state(store)
    elif op == "context.history":
        items = []
        item = workspace.revision(store, args["after"])
        if args["after"] is not None and item["parent"] is not None:
            item = workspace.revision(store, item["parent"])
        while len(items) < 25:
            items.append({key: value for key, value in item.items() if key != "config"})
            if item["parent"] is None:
                break
            item = workspace.revision(store, item["parent"])
        result = {"items": items, "after": items[-1]["version"] if items[-1]["parent"] else None}
    elif op == "context.preview_config":
        scene = args["scene"]
        if scene not in workspace.SCENES:
            raise AssistantError("workspace_config", "请选择一个可用场景")
        view = workspace.state(store)
        workspace.validate_effective(store, args["config"])
        draft = {"version": "preview", "config": args["config"]}
        context = {"event": {"text": "我们上次聊到哪儿了？", "visibility": scene},
                   "runtime": {"read_only": False}, "conversation": {"turns": [], "recalled": [],
                   "summary": {"content": "一起讨论过看展的计划。"}}}
        if scene == "private":
            context["personal_facts"] = {"items": [{"id": "demo-name", "attribute": "preferred_name",
                                                     "status": "active", "value": "小禾"}]}
        reports = {}
        for name, record in (("active", view["active"]), ("draft", draft)):
            material = dict(context)
            workspace.apply_guidance(material, record, scene)
            instructions, schema = DecisionService.specification(material, [])
            _, reports[name] = pack(material, workspace.policy(store.read("companion")["data"], record),
                                    instructions, strict_schema(schema))
        result = {"source": "synthetic", "scene": scene, "reports": reports,
                  "excluded": [] if scene == "private" else [{"layer": "personal_facts", "reason": "private_scope"}]}
    else:
        raise AssistantError("invalid_operation", "未知操作")
    manager.authority.require(request, administrator=True)
    return result
