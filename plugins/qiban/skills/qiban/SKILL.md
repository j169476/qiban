---
name: qiban
description: 栖伴 Qiban 的统一入口。用户通过 @qiban 或 $qiban 管理平台、打开管理页或会客室、查看任务和额度、重启服务时使用；按明确目标转到管理或具体平台技能。
---

# 栖伴 Qiban

网页与 Codex 使用同一本机服务。根据用户意图选择入口；只打开页面不会启动角色。

- 管理、配置、查看额度与任务、重启服务：读取 [qiban-admin](../qiban-admin/SKILL.md)。在未绑定的当前聊天调用 `management_open`，打开返回的 `web.admin`。
- 本机对话或会客室运行：读取 [qiban-local](../qiban-local/SKILL.md)。
- B 站运行：读取 [qiban-bilibili](../qiban-bilibili/SKILL.md)。
- 微信运行：读取 [qiban-wechat](../qiban-wechat/SKILL.md)。
- QQ 运行：读取 [qiban-qq](../qiban-qq/SKILL.md)。

仅提及 Qiban 时展示管理入口与当前状态，不推断用户要启动所有平台。已绑定的管理聊天和角色聊天不互换身份。用户明确要求新建聊天时，使用宿主提供的新建聊天工具，并在新聊天调用对应技能；不能伪造 `CODEX_THREAD_ID`。

管理聊天可调用 `runtime_monitor` 获取本地上限与已用量、Codex coding plan 账户共享额度和 Qiban 已观测 token。三类数据分别解释，缺失值为未知。不要将整个账户的额度变化归因于 Qiban。

退出 Codex 时后台和数据库可能一起结束，网页可显示服务断线；下次启动先检查中断状态，保持暂停，结果不明的发送不自动补发。不承诺每次退出都能执行收尾回调。重新打开后，在已绑定管理聊天按用户明确请求调用 `service_resume`；或由用户在项目目录运行 `python -m qiban restart --project .`。此操作恢复服务可用性，平台仍等待显式启动。网页不能绕过这一步自动恢复。
