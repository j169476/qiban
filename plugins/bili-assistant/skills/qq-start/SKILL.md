---
name: qq-start
description: 仅在用户明确要求启动、继续、停止或查询栖伴的QQ模块时使用。其他平台有独立指令；含糊的启动请求先确认目标平台。
---

# QQ独立启停

使用本技能所在插件上两级目录中的 scripts/run.py，通过项目的 Python 环境连接本机服务。

```text
python <插件绝对路径>/scripts/run.py call qq_start
python <插件绝对路径>/scripts/run.py call qq_status
python <插件绝对路径>/scripts/run.py call qq_stop
```

- 用户明确要求启动或继续QQ才调用启动。只影响QQ，不连带启动其他平台。安装、打开网页、取消归档或重启服务都不构成启动。
- 参数用 --input <UTF-8 JSON 文件绝对路径> 传递，仅支持 read_only 和 duration_minutes（1–1440）。重复启动不会延长截止时间；改变归属或模式须先停止本平台。
- CODEX_THREAD_ID 必须由宿主注入，不能自行填写或借用；共享 MCP 不能代替此 CLI 身份。归档归属聊天会暂停它拥有的运行。
- 平台未装配返回 platform_unavailable，报告具体状态；不得改用其他平台或通用启动指令重试。
- 此入口只控制生命周期，不读取私人记忆、凭据或聊天原文，不扮演角色。按实际状态说明成功、暂停或不可用。
- 独立 OneBot 连接器需先完成本机协议端登录及只读 probe。普通 QQ 已打开或最小化不代表接口可用；qq_configuration、qq_token_required、qq_enrollment_required 表示尚需安装配置或只读核验，不可借用另一个账号或放宽目标。
- 默认未授权会话不回复。私聊按平台机器人账号的独立每日额度直接回应；授权群由真实 @ 或本连接器已核验的同群机器人消息引用唤起，默认最多5条成功回复、20条入场消息判断、10分钟，话题结束提前退出，之后需再次唤起。配置由网页或后台管理聊天的 reply_policy_list/set 管理。
- account_mismatch、目标核验失败、发送不确定时按实际状态暂停；不能自动重发、换号或扩大授权。连接重试只恢复事件连接，重启不继承群参与窗口。没有真实收发证据时不能宣称已验收。说明见项目 docs/QQ_MODULE.md。
