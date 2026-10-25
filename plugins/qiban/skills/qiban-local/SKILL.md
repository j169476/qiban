---
name: qiban-local
description: 仅在用户明确要求启动、继续、停止或查询栖伴的本机会客室模块时使用。其他平台有独立指令；含糊的启动请求先确认目标平台。
---

# 本机会客室独立启停

使用本技能所在插件上两级目录中的 scripts/run.py，通过项目的 Python 环境连接本机服务。

```text
python <插件绝对路径>/scripts/run.py call assistant_start
python <插件绝对路径>/scripts/run.py call assistant_status
python <插件绝对路径>/scripts/run.py call assistant_stop
```

- 用户明确要求启动或继续本机会客室才调用启动。只影响本机会客室，不连带启动其他平台。安装、打开网页、取消归档或重启服务都不构成启动。
- 参数用 --input <UTF-8 JSON 文件绝对路径> 传递，仅支持 read_only 和 duration_minutes（1–1440）。重复启动不会延长截止时间；改变归属或模式须先停止本平台。
- CODEX_THREAD_ID 必须由宿主注入，不能自行填写或借用；共享 MCP 不能代替此 CLI 身份。归档归属聊天会暂停它拥有的运行。
- 返回 setup_required 时使用后台管理入口完成本机网页初始化；不得改用平台启动指令重试。
- 此入口只控制生命周期，不读取私人记忆、凭据或聊天原文，不扮演角色。按实际状态说明成功、暂停或不可用。

成功后打开返回的 web.role，由用户本人登录。此入口只开启本机网页对话和显式任务。
