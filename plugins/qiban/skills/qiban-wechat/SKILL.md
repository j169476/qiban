---
name: qiban-wechat
description: 仅在用户明确要求启动、继续、停止或查询栖伴的微信模块时使用。其他平台有独立指令；含糊的启动请求先确认目标平台。
---

# 微信独立启停

使用本技能所在插件上两级目录中的 scripts/run.py，通过项目的 Python 环境连接本机服务。

```text
python <插件绝对路径>/scripts/run.py call wechat_start
python <插件绝对路径>/scripts/run.py call wechat_status
python <插件绝对路径>/scripts/run.py call wechat_stop
```

- 用户明确要求启动或继续微信才调用启动。只影响微信，不连带启动其他平台。安装、打开网页、取消归档或重启服务都不构成启动。
- 参数用 --input <UTF-8 JSON 文件绝对路径> 传递，仅支持 read_only 和 duration_minutes（1–1440）。重复启动不会延长截止时间；改变归属或模式须先停止本平台。
- CODEX_THREAD_ID 必须由宿主注入，不能自行填写或借用；共享 MCP 不能代替此 CLI 身份。归档归属聊天会暂停它拥有的运行。
- 平台未装配返回 platform_unavailable，报告具体状态；不得改用其他平台或通用启动指令重试。
- 此入口只控制生命周期，不读取私人记忆、凭据或聊天原文，不扮演角色。按实际状态说明成功、暂停或不可用。
- 正式模块采用数据库只读收件和受限原生文本发送。默认不回复任何会话；先在管理网页或已绑定管理聊天明确授权已核验私聊或群聊，支持多对象，按稳定内部 ID 绑定。私聊直接回应且使用独立每日额度；群聊由已核验成员真实 @/引用唤起后有限参与，默认5条回复、20条入场消息判断、10分钟，话题结束可提前退出，之后需重新唤起。旧 family_enabled 配置不授予权限。授权或参与设置变更会暂停微信，须明确重新启动。完整说明见项目 `docs/wechat-module.md` 与 `docs/QQ_MODULE.md`。
- 首次配置、追加对象或切换模型转到 [qiban-admin](../qiban-admin/SKILL.md)。接入向导准备依赖、核验客户端与目标；名单导入先预览后确认。更新发送桥接后如提示 client_restart_required，正常重启对应微信客户端后重试，不注入到其他账号。
- `wechat_status` 的 `connectors` 提供收件重连、家庭群开关和原生状态。`uncertain` 不得解释为失败后可重发；先检查实际收件。`client_restart_required` 表示需正常退出并重开小号微信。发送时小号保持最小化。
- 模块的离线配置和构建检查不代表实机验收通过，也不构成启动授权；服务重载后仍需明确 `wechat_start`。
