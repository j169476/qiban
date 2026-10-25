"""One validated operation table used by CLI and MCP."""

from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import AssistantError
from .learning_tree import NODE_CONFIG
from .schema import ACTIONS, CONFIG, EMOTION_CHANGE, GOAL_CRITERIA, MEMORY_COLLECTIONS, SCHEMAS, obj
from .storage import utcnow

TEXT = {"type": "string"}
DOC = {"type": "object"}
TOOLS = {
    "reply_policy_list": ("管理端读取QQ/微信的私聊每日额度、群聊参与上限及私聊用量。", obj({})),
    "reply_policy_set": ("管理端修改私聊额度与群聊参与参数，版本冲突需重读，变更暂停对应平台。", obj({
        "platform": {"enum": ["qq", "wechat"]}, "account_identity": TEXT,
        "changes": {"type": "object", "minProperties": 1, "additionalProperties": False, "properties": {
            "private_daily_replies": {"type": "integer", "minimum": 0, "maximum": 10000},
            "group_session_replies": {"type": "integer", "minimum": 1, "maximum": 100},
            "group_session_decisions": {"type": "integer", "minimum": 1, "maximum": 1000},
            "group_session_minutes": {"type": "integer", "minimum": 1, "maximum": 120}}},
        "expected_version": {"type": ["string", "null"]}})),
    "reply_authorization_list": ("已绑定管理聊天读取微信/QQ的已核验会话及回复授权；不读取聊天内容、不启动角色。", obj({})),
    "reply_authorization_set": ("管理端按明确指令授权或撤销会话回复；先读取列表取得ID和版本。变更暂停对应平台。", obj({
        "scope_id": TEXT, "enabled": {"type": "boolean"}, "expected_version": {"type": ["string", "null"]},
    })),
    "service_setup": ("已迁移项目的后台管理聊天获取一次性网页初始化链接；不启动角色。", obj({})),
    "identity_status": ("管理端检查可撤销的跨平台身份候选、硬关联及证据状态，不启动角色。", obj({})),
    "identity_revoke": ("管理端撤销错误身份关联，保留原始人物印象和聊天记录。", obj({
        "link_id": TEXT, "expected_version": TEXT, "reason": TEXT,
    })),
    "identity_propose": ("角色根据实际入站事件证据提出身份关联；核心校验后写入，不接受昵称匹配作为硬关联。", obj({
        "proposal": DOC,
    })),
    "identity_context": ("角色读取指定主体的核心身份候选及来源证据，供当前 Codex 判断，不发送消息。", obj({
        "platform": TEXT, "user_id": TEXT, "scope": TEXT,
    }, ["platform", "user_id"])),
    "person_profile": ("查看带来源的人物映像；身份推断不授予管理权限。", obj({
        "platform": TEXT, "user_id": TEXT, "scope": TEXT,
    }, ["platform", "user_id"])),
    "core_migrate": ("管理端在角色暂停时补充旧数据的核心主体/事件映射，不重写原始账本或启动运行。", obj({})),
    "native_task_create": ("角色为具体资料问题排队原生 web 任务，或为动态草稿排队生成与审图；受后台额度和运行状态约束。", obj({
        "kind": {"enum": ["web", "image"]}, "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
        "goal_id": TEXT, "draft": {"type": "string", "maxLength": 1000},
        "use_persona": {"type": "boolean"},
    }, ["kind", "prompt"])),
    "native_task_status": ("查看持久化原生任务、来源证据和当天工具额度；不会启动后台。", obj({})),
    "native_task_control": ("取消原生任务，或由角色重试失败任务；有动态发送记录时不可自动重试。", obj({
        "job_id": TEXT, "action": {"enum": ["cancel", "retry"]}, "expected_version": TEXT,
    })),
    "login_browser_prepare": ("管理聊天发起 15 分钟内有效的浏览器登录任务；角色须已暂停，不接收 Cookie 参数。", obj({})),
    "login_browser_status": ("查看本管理聊天的登录任务及脱敏结果，过期时清理暂存密文。", obj({
        "ticket_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
    }, [])),
    "login_browser_finish": ("管理聊天验证已暂存的网页登录态，同账号才更新系统凭证库；不启动角色。", obj({
        "ticket_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
    })),
    "login_browser_cancel": ("管理聊天取消本次网页登录并清理暂存密文，已完成的登录不撤回。", obj({
        "ticket_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
    })),
    "learning_tree_status": ("查看目标知识树、各点模型自检掌握度、错题和到期复习；不启动角色。", obj({"goal_id": TEXT})),
    "learning_node_configure": ("设置同一目标内知识点的父节点和先修依赖，检查缺失引用及环；不改测验成绩。", NODE_CONFIG),
    "learning_goal_attach": (
        "把已保存且有来源的知识加入显式学习目标，供原有测验闭环使用；不启动角色，已完成目标转为暂停待确认。",
        obj({"goal_id": TEXT, "knowledge_id": TEXT, "expected_version": TEXT}),
    ),
    "web_source_capture": (
        "角色聊天用 Codex 原生 web 检索并实际打开来源后，保存原样摘录和工具引用；不是搜索标题，也不声称插件独立验证网页。",
        obj({"url": {"type": "string", "maxLength": 3000}, "title": {"type": "string", "maxLength": 500},
             "excerpt": {"type": "string", "minLength": 1, "maxLength": 12000},
             "tool_reference": {"type": "string", "minLength": 1, "maxLength": 1000}}),
    ),
    "web_note_save": (
        "基于已保存网页摘录写知识笔记；quotes 必须逐字出现在指定版本的材料中，随后进入统一知识复查和召回。",
        obj({"source_id": TEXT, "source_version": TEXT, "content": {"type": "string", "minLength": 1, "maxLength": 12000},
             "quotes": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string", "maxLength": 500}},
             "tags": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 100}}}),
    ),
    "research_export": (
        "把指定知识和可选学习树导出到本地 exports，带实际来源、覆盖、复查和版本清单。支持报告、知识卡和思维导图；不发布。",
        obj({"title": {"type": "string", "minLength": 1, "maxLength": 200},
             "knowledge_ids": {"type": "array", "maxItems": 30, "items": TEXT},
             "format": {"enum": ["markdown", "html", "docx", "mindmap"]},
             "style": {"enum": ["report", "cards"]}, "goal_id": TEXT}, ["title", "knowledge_ids"]),
    ),
    "publication_image_import": (
        "把用户提供或 Codex 原生 imagegen 生成并审阅的本地图片导入发布素材，返回 id/version；不上传不发布。",
        obj({"path": TEXT, "origin": {"enum": ["codex_imagegen", "user_provided"]},
             "description": {"type": "string", "maxLength": 2000}}),
    ),
    "management_open": ("绑定本聊天为管理入口，加载配置，不启动角色。", obj({})),
    "assistant_start": (
        "显式启动本机会客室；服务模式下不会启动 QQ、微信或 B站，可选限时和只读。",
        obj({"background": {"type": "boolean"},
             "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
             "read_only": {"type": "boolean"}}, []),
    ),
    "assistant_status": ("查看运行、已用额度和文件校验状态。", obj({})),
    "runtime_metrics": ("管理端按需查看核心阶段、Codex 调用耗时和已观测 token；缺失用量未知，不启动角色。", obj({
        "days": {"type": "integer", "minimum": 1, "maximum": 30},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    }, [])),
    "discovery_status": ("查看搜索额度、实际阅读主题与作者集中度，不触发搜索或模型。", obj({})),
    "knowledge_status": ("查看知识复查、未决冲突与材料覆盖；不启动后台。", obj({})),
    "bili_video_interaction_status": (
        "只读核对视频是否点赞及累计投币数量，供排错和核查不确定动作；不启动角色。",
        obj({"bvid": {"type": "string", "pattern": "^BV[A-Za-z0-9]{10}$"}}),
    ),
    "learning_goal_create": (
        "仅在用户明确要求学习某部分知识时创建目标；引用本角色聊天真实原话。兴趣、推荐、纠正不构成学习指令。",
        obj({"objective": {"type": "string", "minLength": 1, "maxLength": 3000},
             "criteria": GOAL_CRITERIA, "source_turn_id": TEXT, "request_quote": TEXT}),
    ),
    "learning_goal_status": (
        "查看显式目标、阶段、缺口和测验；不会启动助手或创建目标。", obj({"goal_id": TEXT}, []),
    ),
    "learning_goal_control": (
        "角色暂停、继续或取消已有学习目标；需要当前文件版本，不会启动助手后台。",
        obj({"goal_id": TEXT, "action": {"enum": ["pause", "resume", "cancel"]}, "expected_version": TEXT}),
    ),
    "method_trial_record": (
        "角色记录方法的具体推演或用户使用反馈；用户反馈须引用本聊天已同步的真实用户消息，不冒充独立实测。",
        obj({
            "skill_id": TEXT, "mode": {"enum": ["simulation", "user_report"]},
            "case": TEXT, "expected": TEXT, "observed": TEXT,
            "outcome": {"enum": ["passed", "failed", "inconclusive"]},
            "limitations": TEXT, "evidence_id": TEXT, "evidence_quote": TEXT,
        }),
    ),
    "assistant_pause": ("暂停本机会客室；服务模式下其他平台不变。", obj({})),
    "assistant_stop": ("停止本机会客室；服务模式下其他平台不变。", obj({})),
    "assistant_stop_all": ("已绑定管理聊天显式停止本机会客室与全部平台。", obj({})),
    "config_get": (
        "读取管理配置和内容哈希。",
        obj({"kind": {"enum": list(CONFIG)}}),
    ),
    "config_patch": (
        "管理聊天修改配置；expected_version 必须来自刚读取的文件。",
        obj(
            {
                "kind": {"enum": list(CONFIG)},
                "patch": DOC,
                "expected_version": TEXT,
            }
        ),
    ),
    "config_validate": ("校验待保存配置，不写入。", obj({"kind": {"enum": list(SCHEMAS)}, "data": DOC})),
    "state_load": ("按当前聊天模式加载本地状态。", obj({})),
    "state_validate": ("校验所有本地文件并列出具体问题。", obj({})),
    "memory_search": ("召回相关记忆、知识、技能卡；可传 Codex 生成的 queries 扩词和关系档案 uids；空查询按记录时间排序。", obj({
        "query": TEXT,
        "queries": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 5},
        "uids": {"type": "array", "maxItems": 20, "items": {"type": "string", "pattern": "^[0-9]{1,20}$"}},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    }, ["query"])),
    "memory_context": (
        "取得有大小上限的记忆正文和目录，由当前 Codex 关联和选择相关内容。",
        obj({"query": TEXT,
             "uids": {"type": "array", "maxItems": 20,
                      "items": {"type": "string", "pattern": "^[0-9]{1,20}$"}},
             "queries": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 5}}, []),
    ),
    "memory_catalog": (
        "分页读取有效语义分组、原始来源和全库整理覆盖率；当前 Codex 可按含义选组后 memory_get。",
        obj({"cursor": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []),
    ),
    "memory_get": (
        "读取目录中某条记忆、知识、关系、日记或技能卡的完整最新内容。",
        obj({"kind": {"enum": list(MEMORY_COLLECTIONS)}, "id": TEXT}),
    ),
    "memory_write": (
        "角色保存记忆、知识、成长、日记、技能卡或有真实来源的兴趣线索；不能修改管理配置。",
        obj(
            {
                "kind": {"enum": ["memories", "knowledge", "growth", "diaries", "skills", "interest_signals"]},
                "data": DOC,
                "expected_version": {"type": ["string", "null"]},
            },
            ["kind", "data"],
        ),
    ),
    "relationship_update": (
        "按一次真实经历更新关系，好感变化最多 3，事件 ID 去重。",
        obj(
            {
                "uid": TEXT, "display_name": TEXT, "delta": {"type": "integer"}, "event_id": TEXT,
                "impression": {"type": "string", "maxLength": 2000},
                "facts": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 10},
            },
            ["uid", "display_name", "delta", "event_id"],
        ),
    ),
    "conversation_sync": (
        "通过官方宿主接口保存本角色聊天已完成轮次的发言；不读取管理或其他聊天、不启动后台。", obj({}),
    ),
    "conversation_context": (
        "读取当前角色聊天或真实入站事件所属线程的近期原文；失败和未知发送明确标记。",
        obj({"event_id": TEXT}, []),
    ),
    "emotion_update": (
        "根据已同步的真实用户消息小幅更新短期情绪；evidence_ids 使用 conversation_context 的条目 ID。",
        obj({"proposal": EMOTION_CHANGE, "expected_version": TEXT}),
    ),
    "bili_search": ("搜索 B站视频候选，标题简介不代表完整视频内容。", obj({"query": TEXT})),
    "bili_feed": ("读取关注更新或推荐视频。", obj({"following": {"type": "boolean"}}, [])),
    "bili_video_context": (
        "获取字幕与原生视觉输入；缺字幕时尝试本地转写。detail 可补读音轨和时间画面，start_seconds 指定片段起点。",
        obj({"bvid": TEXT, "page_index": {"type": "integer", "minimum": 0}, "detail": {"type": "boolean"},
             "start_seconds": {"type": "number", "minimum": 0},
             "end_seconds": {"type": "number", "minimum": 0}}, ["bvid"]),
    ),
    "video_reading_status": ("查看指定视频分 P 的文字覆盖、缺口、画面采样区间及补读任务；不启动后台。", obj({
        "bvid": TEXT, "page_index": {"type": "integer", "minimum": 0},
    }, ["bvid"])),
    "video_gap_request": ("角色针对已读取视频的具体缺口安排补读；逐轮限额处理，原视频不重复计数。", obj({
        "bvid": TEXT, "page_index": {"type": "integer", "minimum": 0},
        "start_seconds": {"type": "number", "minimum": 0}, "end_seconds": {"type": "number", "minimum": 0},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000}, "mode": {"enum": ["text", "visual"]},
    }, ["bvid", "start_seconds", "end_seconds", "reason"])),
    "media_status": ("只读检查 FFmpeg、转写组件及本地模型是否就绪，不下载模型或启动角色。", obj({})),
    "bili_inbox": ("只读取启动之后收到的视频评论回复、提及和私信事件。", obj({})),
    "bili_event_context": (
        "读取实际入站事件的图片、分享卡与分页评论楼；历史楼内评论只作上下文，不授予回复资格。",
        obj({"event_id": TEXT, "page": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["event_id"]),
    ),
    "action_prepare": (
        "内部校验和持久去重；retry_failed 重试明确拒绝，retry_unsent 重建同归属的未发送/已取消草稿；未知结果不能重发。",
        obj(
            {"kind": {"enum": list(ACTIONS)}, "payload": DOC, "retry_failed": {"type": "boolean"},
             "retry_unsent": {"type": "boolean"}},
            ["kind", "payload"],
        ),
    ),
    "action_execute": ("按最新权限和额度发送一次动作；不重试未知结果。", obj({"action_id": TEXT})),
    "action_status": ("检查动作状态，uncertain 不能自动重发。", obj({"action_id": TEXT})),
    "data_backup": ("创建完整备份，含关系去重、额度和动作账本；不包含登录凭证。", obj({})),
    "data_restore": (
        "管理入口将完整备份恢复到尚不存在的新目录，强制暂停，不自动切换当前项目。",
        obj({"archive": TEXT, "destination": TEXT}),
    ),
    "relationship_recovery_status": ("检查待落盘关系提交与人工编辑冲突。", obj({})),
    "relationship_recovery_resolve": (
        "管理入口在角色暂停时裁决关系提交冲突；保留本地版本或应用待提交版本，均不会重复计分。",
        obj({"uid": TEXT, "choice": {"enum": ["keep_local", "apply_pending"]},
             "expected_version": {"anyOf": [TEXT, {"type": "null"}]}}),
    ),
    "data_export": ("导出权威配置及内容文件，不包含凭证或运行数据库。", obj({})),
    "index_rebuild": ("从权威 YAML 重建可删除的检索索引。", obj({})),
}
READ_ONLY = {
    "runtime_metrics",
    "identity_status",
    "identity_context",
    "person_profile",
    "learning_tree_status",
    "media_status",
    "assistant_status",
    "discovery_status",
    "knowledge_status",
    "bili_video_interaction_status",
    "learning_goal_status",
    "config_get",
    "config_validate",
    "state_load",
    "state_validate",
    "memory_search",
    "memory_context",
    "memory_catalog",
    "memory_get",
    "conversation_context",
    "bili_search",
    "bili_feed",
    "bili_video_context",
    "bili_inbox",
    "bili_event_context",
    "action_status",
    "relationship_recovery_status",
}


for _platform in ("bili", "wechat", "qq"):
    TOOLS[_platform + "_start"] = ("仅显式启动指定平台，其他平台和本机会客室不变。", obj({
        "read_only": {"type": "boolean"},
        "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440}}, []))
    for _action in ("stop", "pause", "status"):
        TOOLS[_platform + "_" + _action] = ("仅控制或查询指定平台。", obj({}))

async def call(engine, name, arguments):
    if getattr(engine.store, "backend", None) == "postgres":
        raise AssistantError("service_entry_required", "数据库资料由本机服务接管，禁止旧入口直接加载角色上下文")
    if name in {"assistant_stop_all", "reply_authorization_list", "reply_authorization_set",
                "reply_policy_list", "reply_policy_set"} or name in {
            p + "_" + a for p in ("bili", "wechat", "qq") for a in ("start", "stop", "pause", "status")}:
        raise AssistantError("service_entry_required", "平台独立启停需要本机服务入口")
    if name not in TOOLS:
        raise AssistantError("unknown_tool", name)
    if list(Draft202012Validator(TOOLS[name][1]).iter_errors(arguments)):
        raise AssistantError("invalid_arguments", f"{name} 参数不符合工具定义")
    if name in ("identity_status", "identity_revoke", "core_migrate"):
        engine.mode("admin")
        from companion_core.identity import IdentityService
        if name == "identity_status":
            return {"links": IdentityService(engine.store).links(),
                    "recent_rejections": engine.store.state("identity-rejections", [])}
        if name == "identity_revoke":
            return IdentityService(engine.store).revoke(**arguments)
        if engine.store.state("run", {}).get("status") == "active":
            raise AssistantError("stop_required", "先暂停角色再迁移")
        from .connector import migrate_legacy
        return migrate_legacy(engine.store)
    if name == "person_profile":
        engine.mode()
        from companion_core.contracts import ActorRef
        from companion_core.people import profile
        return profile(engine.store, ActorRef(**arguments))
    if name == "identity_context":
        engine.mode("role")
        from companion_core.contracts import ActorRef
        from companion_core.identity import IdentityService
        return IdentityService(engine.store).context(ActorRef(**arguments), include_evidence=True)
    if name == "identity_propose":
        engine.role_run()
        from companion_core.identity import IdentityService
        return IdentityService(engine.store).apply(arguments["proposal"])
    if name.startswith("native_task_"):
        from .native_tools import control, create, status

        return {"native_task_create": create, "native_task_status": status,
                "native_task_control": control}[name](engine, **arguments)
    if name.startswith("login_browser_"):
        from . import browser_login

        if name == "login_browser_finish":
            return await browser_login.finish(engine, **arguments)
        return {"login_browser_prepare": browser_login.prepare, "login_browser_status": browser_login.status,
                "login_browser_cancel": browser_login.cancel}[name](engine, **arguments)
    if name in ("learning_tree_status", "learning_node_configure"):
        from .learning_tree import configure, status

        return (status if name == "learning_tree_status" else configure)(engine, **arguments)
    if name in ("web_source_capture", "web_note_save", "research_export", "learning_goal_attach"):
        from .research import attach, capture, export, save_note

        return {"web_source_capture": capture, "web_note_save": save_note, "research_export": export,
                "learning_goal_attach": attach}[name](engine, **arguments)
    if name == "publication_image_import":
        from .publication import import_image

        return import_image(engine, **arguments)
    if name == "management_open":
        result = engine.open("admin")
        from .health import start_notifier

        result["fault_notifier_started"] = start_notifier(engine)
        return result
    if name == "assistant_start":
        from .scheduler import launch, readiness

        result = engine.start(arguments.get("duration_minutes"), arguments.get("read_only"))
        result["state"] = engine.load()
        result["background"] = launch(engine) if arguments.get("background", True) else readiness(engine)
        return result
    if name == "assistant_status":
        return engine.status()
    if name == "runtime_metrics":
        engine.mode("admin")
        from companion_core.observability import report

        return report(engine.store, **arguments)
    if name == "bili_video_interaction_status":
        engine.mode()
        me = await engine.adapter.me()
        if str(me["uid"]) != engine.store.read("manifest")["data"]["account_uid"]:
            raise AssistantError("account_mismatch", "账号与绑定 UID 不一致")
        return await engine.adapter.video_interaction_status(**arguments)
    if name == "discovery_status":
        from .discovery import diversity, search_usage

        engine.mode()
        return {"search_quota": search_usage(engine), "diversity": diversity(engine)}
    if name == "knowledge_status":
        from .knowledge import status

        return status(engine)
    if name.startswith("learning_goal_"):
        from .learning import control, create, status

        return {"learning_goal_create": create, "learning_goal_control": control,
                "learning_goal_status": status}[name](engine, **arguments)
    if name == "method_trial_record":
        from .knowledge import record_trial

        return record_trial(engine, **arguments)
    if name == "assistant_pause":
        return engine.stop(paused=True)
    if name == "assistant_stop":
        return engine.stop()
    if name == "config_get":
        return engine.config_get(**arguments)
    if name == "config_patch":
        return engine.config_patch(**arguments)
    if name == "config_validate":
        from .schema import validate

        engine.mode()
        validate(**arguments)
        return {"valid": True}
    if name == "state_load":
        return engine.load()
    if name == "state_validate":
        engine.mode()
        return engine.store.validate_all()
    if name == "memory_search":
        engine.mode("role")
        return engine.store.search(arguments["query"], arguments.get("limit", 10),
                                   queries=arguments.get("queries", []), uids=arguments.get("uids", []))
    if name == "memory_context":
        engine.mode("role")
        return engine.store.context(arguments.get("query", ""), queries=arguments.get("queries", []),
                                    uids=arguments.get("uids", []))
    if name == "memory_catalog":
        from .memory_maintenance import catalog

        engine.mode("role")
        return catalog(engine.store, **arguments)
    if name == "memory_get":
        engine.mode("role")
        record = engine.store.read(arguments["kind"], arguments["id"])
        if arguments["kind"] == "skills":
            for knowledge_id in record["data"]["knowledge_ids"]:
                engine.store.read("knowledge", knowledge_id)
        from .knowledge import annotations
        from .persona import annotate_relationship

        return annotate_relationship(annotations(engine.store, [{"kind": arguments["kind"], **record}])[0],
                                     engine.store.read("persona")["data"])
    if name == "memory_write":
        return engine.remember(**arguments)
    if name == "relationship_update":
        return engine.relationship(**arguments)
    if name == "conversation_sync":
        from .conversations import sync_codex

        return sync_codex(engine)
    if name == "conversation_context":
        return engine.conversation_context(**arguments)
    if name == "emotion_update":
        return engine.emotion_update(**arguments)
    if name in ("bili_search", "bili_feed"):
        engine.role_run()
        if name == "bili_search":
            from .discovery import search

            return await search(engine, **arguments)
        return await engine.adapter.feed(**arguments)
    if name == "media_status":
        engine.mode()
        from .video_material import readiness

        return readiness(engine.store)
    if name == "bili_video_context":
        return await engine.video_context(**arguments)
    if name in ("video_reading_status", "video_gap_request"):
        from .video_progress import request, status

        return (status if name == "video_reading_status" else request)(engine, **arguments)
    if name == "bili_inbox":
        return await engine.inbox()
    if name == "bili_event_context":
        from .rich_content import event_context

        return await event_context(engine, **arguments)
    if name == "action_prepare":
        return engine.prepare(**arguments)
    if name == "action_execute":
        return await engine.execute(**arguments)
    if name == "action_status":
        engine.mode()
        with engine.store.db() as db:
            row = db.execute(
                "SELECT id,kind,status,created,dispatched,result FROM actions WHERE id=?",
                (arguments["action_id"],),
            ).fetchone()
        if not row:
            raise AssistantError("not_found", "未找到动作")
        return dict(row)
    engine.mode()
    if name == "relationship_recovery_status":
        from .relationship_store import status

        return {"pending": status(engine.store)}
    if name in ("data_restore", "relationship_recovery_resolve"):
        engine.mode("admin")
        if (engine.store.state("run") or {}).get("status") == "active":
            raise AssistantError("pause_required", "先暂停角色，再进行恢复或冲突裁决")
        if name == "relationship_recovery_resolve":
            from .relationship_store import resolve

            return resolve(engine.store, arguments["uid"], arguments["choice"], arguments["expected_version"])
        from .backups import restore

        destination = Path(arguments["destination"]).resolve()
        if destination.is_relative_to(engine.store.root):
            raise AssistantError("restore_destination_invalid", "恢复目录应在当前数据目录之外")
        return restore(Path(arguments["archive"]), destination)
    if name == "data_backup":
        return engine.store.backup(force=True)
    if name == "data_export":
        path = engine.store.root / "history" / ("export-" + utcnow()[:10] + ".zip")
        return engine.store.export(path)
    if name == "index_rebuild":
        return engine.store.rebuild_index()
    raise AssistantError("unknown_tool", name)
