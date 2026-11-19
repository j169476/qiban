"""Application reply settings. Revisions are immutable; each reply pins one."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from .context_budget import PROPERTIES
from .errors import AssistantError

HEAD = "reply-workspace:head"
DRAFT = "reply-workspace:draft"
PREFIX = "reply-workspace:version:"
SCENES = ("private", "group", "public")
LAYERS = ("memory_records", "summary", "relations", "growth", "catalog", "files", "stickers")
DEFAULT = {"guidance": {}, "limits": {}, "order": list(LAYERS)}


def revision(store, version=None, db=None):
    version = store.state(HEAD, "initial", db) if version is None else version
    if version == "initial":
        return {"version": version, "parent": None, "config": deepcopy(DEFAULT), "at": None, "actor": None}
    if not isinstance(version, str) or len(version) != 32:
        raise AssistantError("workspace_version", "找不到该配置版本")
    record = store.state(PREFIX + version, None, db)
    if record is None:
        raise AssistantError("workspace_version", "找不到该配置版本")
    return record


def validate_config(value):
    if not isinstance(value, dict) or set(value) - {"disabled"} != set(DEFAULT):
        raise AssistantError("workspace_config", "配置字段有误")
    guidance, limits, order = value["guidance"], value["limits"], value["order"]
    disabled = value.get("disabled", [])
    if (not isinstance(disabled, list) or len(disabled) > len(LAYERS)
            or any(v not in LAYERS for v in disabled) or len(set(disabled)) != len(disabled)):
        raise AssistantError("workspace_config", "停用的资料层有误")
    if (not isinstance(guidance, dict) or set(guidance) - set(SCENES)
            or any(not isinstance(v, str) or len(v) > 2000 for v in guidance.values())
            or not isinstance(limits, dict) or set(limits) - set(PROPERTIES)
            or not isinstance(order, list) or len(order) != len(LAYERS)
            or not all(isinstance(v, str) for v in order) or set(order) != set(LAYERS)):
        raise AssistantError("workspace_config", "场景说明、预算或资料顺序有误")
    for key, number in limits.items():
        if type(number) is not int or not PROPERTIES[key]["minimum"] <= number <= PROPERTIES[key]["maximum"]:
            raise AssistantError("workspace_config", "预算超出可用范围")


def state(store, db=None):
    active = revision(store, db=db)
    draft = store.state(DRAFT, None, db) or {"base": active["version"], "revision": "initial", "config": active["config"]}
    return {"active": active, "draft": draft, "scenes": list(SCENES), "layers": list(LAYERS)}


def edit(store, action, args, actor, authorize):
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        authorize()
        if store.state("desktop-last-restore", {}, db).get("status") == "assets_pending":
            raise AssistantError("restore_pending", "资料正在恢复")
        view = state(store, db)
        draft, head = view["draft"], view["active"]["version"]
        if args["expected"] != draft["revision"] or args["base"] != head:
            raise AssistantError("edit_conflict", "配置已变更，请重新读取草稿")
        if action == "save":
            config = args["config"]
        elif action == "rollback":
            config = revision(store, args["version"], db)["config"]
        else:
            config = draft["config"]
        validate_config(config)
        from .context_budget import DEFAULTS
        effective = {**DEFAULTS, **store.read("companion")["data"], **config["limits"]}
        if effective["reply_context_tokens"] <= effective["reply_output_reserve"]:
            raise AssistantError("workspace_config", "输出预留必须小于总预算")
        changed = sorted(k for k in set(config) | set(view["active"]["config"])
                         if config.get(k) != view["active"]["config"].get(k))
        if action != "save" and (changed or action == "rollback" and args["version"] != head):
            version = uuid4().hex
            store.set_state(PREFIX + version, {"version": version, "parent": head, "config": config,
                            "actor": actor, "at": datetime.now(timezone.utc).isoformat(),
                            "action": action, "changed": changed, "restored_from": args.get("version")}, db)
            head = version
            store.set_state(HEAD, head, db)
        if draft["config"] != config or draft["base"] != head:
            store.set_state(DRAFT, {"config": config, "base": head, "revision": uuid4().hex}, db)
        return state(store, db)


def policy(settings, pinned):
    config = pinned["config"]
    return {**settings, **config["limits"], "workspace_order": config["order"],
            "workspace_disabled": config.get("disabled", []), "workspace_version": pinned["version"]}


def apply_guidance(context, pinned, visibility):
    text = pinned["config"]["guidance"].get(visibility)
    if text:
        context["application_reply_guidance"] = text
