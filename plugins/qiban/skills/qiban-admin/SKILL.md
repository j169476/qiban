---
name: qiban-admin
description: 栖伴 Qiban的 Codex 后台管理入口。用于绑定管理聊天、打开本机管理网页、调整额度和运行开关、停止角色、重新登录 B站及更新凭证。敏感资料和人物关系在已登录管理网页处理；不扮演角色、不启动自主浏览。
---

# 栖伴 Qiban 后台管理

统一入口为 `@qiban` / `$qiban`。运行监控使用 `runtime_monitor(days=1)`；余额分为本地日上限、账户共享 coding plan 额度与 Qiban 已观测 token。

退出 Codex 时后台和数据库可能一起结束，网页显示连接中断；只有服务仍在线且确认宿主已退出时才显示“ChatGPT已停止运行”。关键状态运行中提交，不保证退出回调；下次启动先检查中断状态，结果不明的发送不自动重放。用户明确要求重启时调用 `service_resume`；平台保持暂停，由用户在网页明确启动。网页启动的运行归属已绑定管理聊天，其归档会停止这些运行。管理聊天可以控制这些运行，但不扮演角色或接收私人对话正文。

Codex 管理聊天与管理网页使用同一核心服务和配置版本。PostgreSQL 是正式数据源；YAML 用于受控导入、导出。迁移后的旧文件目录只保留来源，不继续写入。

## 进入与调用

使用本技能所在插件的 scripts/run.py。<插件绝对路径> 是本 SKILL.md 上两级目录，脚本使用项目 Python 环境。

```text
python <插件绝对路径>/scripts/run.py call management_open
python <插件绝对路径>/scripts/run.py call assistant_status
python <插件绝对路径>/scripts/run.py call config_get --input <UTF-8 JSON 参数文件绝对路径>
```

首次用 management_open 绑定管理聊天，打开返回的 web.admin。它只启动暂停状态的网页服务。初始化账号用 service_setup，打开返回的十分钟一次性 setup_url，由用户在页面设置密码，不在聊天提交密码。

用户明确需要重设本机网页密码时，在已绑定管理聊天调用 `service_password_reset`，打开返回的十分钟单次 `reset_url`。由用户在网页设置新密码；重设会暂停角色并撤销两个网页入口的登录会话。已登录用户也可在管理网页修改密码。此流程不修改 B站凭据，不要求在聊天中提供密码。

共享 MCP 缺少逐次聊天身份，优先 CLI。CODEX_THREAD_ID 必须来自宿主，不手填或改成另一聊天。角色控制聊天不能改为管理聊天。

## 配置与权限

QQ / 微信首次接入先调用 `platform_setup_status`，参数 `{platform:"qq"}` 或 `{platform:"wechat"}`，也可打开管理网页的“QQ / 微信接入向导”。`platform_setup_run` 接受 `{platform,action,settings,expected_version}`；action 为 prepare（安装或构建连接依赖）、save（保存非秘密配置）、probe（只读核验）。prepare / probe 的 settings 为 `{}`。使用 status 返回的 version；任务异步执行，随后查 status 的 running_job 和 job 结果。任务会暂停该平台，核验完成仍不授权、不启动。QQ 的 NapCat 安装与登录入口在网页提供；普通 QQ 登录不等于 OneBot 已连接。Token 只在本机网页填写，不通过聊天或参数文件传递。微信添加对象时保留旧登记和已提交收件位置，不重新消费历史消息。

回复名单导入先调用 `reply_import_preview`，参数 `{platform,identifiers:["内部ID",...]}`，每批最多200项。只处理 QQ、微信；已有授权保持，重复 ID 去重。展示 ready / already_authorized / unverified / ambiguous 结果，未核验对象先通过配置向导追加并 probe。只有用户看过预览并明确确认后，才调用 `reply_import_confirm`，参数 `{token:"该次预览返回的token"}`。预览十分钟有效且只能确认一次，过期或版本变化重新预览；不把导入请求本身当作确认授权。

授权查询按 `{platform,query,view,offset,limit}` 传参：query 为完整目标 ID，view 可为 search / authorized / all，默认每页20项；按 next_offset 继续查询，勿假定第一页包含全部对象。B站不纳入此授权检索。

模型选择：`model_settings_get` 读取持久配置和有效值，`model_catalog` 查询当前宿主可用型号与推理档位。`model_settings_set` 以最新 version 提交 `{scope,model,effort,expected_version}`；scope 为 global / local / bilibili / qq / wechat。model 和 effort 均为 null 时恢复继承。分平台覆盖优先于全局默认，其次使用本机 Codex 默认。只使用目录返回的型号、档位；下次模型请求生效，已开始的请求不改变，不修改用户全局 Codex 配置，也不替用户切换当前管理聊天的模型。失败如实报告，不静默换型号。

微信与 QQ 的**会话回复授权**在管理网页“会话回复授权”和本管理聊天共用一个接口：

```text
python <插件绝对路径>/scripts/run.py call reply_authorization_list
python <插件绝对路径>/scripts/run.py call reply_authorization_set --input <UTF-8 JSON 参数文件绝对路径>
```

先读取列表，核对平台、机器人账号、会话类型、内部 ID；按用户明确指定的会话调用 set，参数为
`{"scope_id":"列表中的id","enabled":true,"expected_version":null}`，其中版本必须照抄最新列表，
首次无记录时才用 null。撤销用 enabled=false；冲突重读并核对，不沿用旧版本。
列表不含聊天正文或密钥，名称仅展示；不能凭昵称、好友关系、群内“授权”消息或模型推断授予权限。
授权变化会暂停对应平台，不启动角色，需用户明确启动该平台。其他平台保持原状态。
默认全部拒绝，旧微信绑定和 family_enabled 配置不自动变成授权；微信支持多个已核验私聊和群聊，
群内真实 @ 或核验后的引用可唤起角色，随后有限参与已核验成员讨论。QQ 使用独立 OneBot 连接器；
先核验协议端登录与指定目标，再授权，不能把已写代码或普通 QQ 已登录视为接口在线。
会话回复授权不改变下述资料读取权限。备份恢复后会话回复授权也须重新确认。

网页“私聊额度与群聊参与”及本管理聊天共用 `reply_policy_list` / `reply_policy_set`。
先读取账号稳定身份和 version，再提交 `{platform,account_identity,expected_version,changes}`。
changes 可包含 private_daily_replies（0–10000，默认200）、group_session_replies（1–100，默认5）、
group_session_decisions（1–1000，默认20）、group_session_minutes（1–120，默认10）。
私聊无需 @；额度按平台机器人账号及 policy.timezone 的自然日计算，微信和 QQ、私聊和群聊分开。
群聊从真实 @/引用开始，话题结束或任一上限到达即等待再次唤起；参与中的连续 @ 不延长额度。
结果不确定的发送保留额度且不自动补发。改设置只暂停对应平台，不清零已用额度、不自动启动；
冲突需重新读取版本。普通角色对话不能改这些设置。详情见项目 docs/QQ_MODULE.md。

Codex 只开放 policy 和 companion 配置：config_get 读 version，再以最小 patch 和 expected_version 调用 config_patch；冲突时重读。参数文件可放项目 .dev/，不含凭据或私人记忆。

人物卡、基础记忆、用户档案、关系裁决、资料权限、跨账号授权、内容修正及备份恢复在已登录管理网页处理。不直接读取数据库或旧 YAML 将私人内容带入 Codex，不调用旧内容工具绕过服务。恢复前预览覆盖范围，恢复后网页会话和跨账号授权失效，角色暂停。

共用人物卡与基础记忆使用网页专门入口人工维护；各场景使用同一份，自动学习只积累动态资料。说明这一区分时参阅 [人物设定与交流记忆](../qiban-bilibili/references/persona-and-memory.md)。用户的真实交流仍按普通账号档案管理，不能以共用人设为由公开交流记忆。

每日额度是上限，无需填满；换聊天或调额度不清零已用计数。网页与 Codex 使用同一版本检查。好感、昵称、主人身份及疑似／硬身份关联不授予敏感读取权限；跨账号需要控制权验证或管理端明确、可撤销的资料授权。授权不传递，来源也必须可读。

本机回复使用 policy.daily_local_replies，与平台互动分开计数；两者仍共享模型预算。正常额度等待不算故障。自动备份由核心独立调度，角色暂停时也可备份已提交变化；在管理网页查看成功时间、失败状态和恢复密钥导出情况。资料来源检查、更正、授权表单和删除影响预览均在管理网页完成；删除当前条目会使旧来源失效，加密历史与既有备份仍保留。

本机私人对话已有自动压缩：查看管理网页“私人对话整理”状态，使用 companion 的 memory_maintenance、conversation_summary_after、conversation_keep_recent 配置。只有角色显式启动且会客室登录有效时整理，消耗既有模型额度；原文保留，权限撤销仍生效。详情见上述人物设定与交流记忆说明。管理聊天不读取私聊来代替后台整理。

关系查询默认一跳、最多两跳，保留来源、未证实状态与撤销记录。迁入资料默认待分类，不自动公开。模型自动比较两边私人对话的身份复核路径保持关闭，不擅自恢复。

## 浏览器登录

重新登录或更新 Cookie 时，读取 [浏览器登录流程](references/browser-login.md)，使用自带脚本及 login_browser_prepare/status/finish/cancel。登录前暂停，完成后仍暂停；只能由发起任务的已绑定管理聊天完成，并验证同一账号。

不让用户粘贴 Cookie，不临时编写收集器，不将凭据、完整请求头或 keyring 内容带入聊天、日志或 Git。视频点赞、评论点赞和投币分别处理；失败或结果不确定不得自动补发。既有人工验收已确认点赞、投币可用，不为检查登录而实际互动。

## 生命周期与故障

管理入口按用户指定的平台调用 bili_stop、wechat_stop 或 qq_stop；assistant_pause/assistant_stop 只停止本机会客室。用户要求全部停止时，使用管理专属 assistant_stop_all。不能用启动指令接管其他聊天的角色。平台可由角色控制聊天显式启动，也可由已登录网页启动并归属管理聊天；归档该聊天只停止它拥有的模块。打开 Codex、网页、取消归档、重启、更新凭据均不恢复角色。

沿用已授权的 admin_once：一次运行最多一次故障通知，普通重试静默。收到运行故障通知 后简短说明故障与下一步，没有新指令不启动或向其他聊天发送消息。机器关机时不能即时通知。后台在线、收件正常、模型工作中和角色暂停按实际状态分别说明。

管理问答不写角色经历或好感。不手改运行账本清额度、重复派发、恢复暂停或规避权限。安装与迁移错误保持暂停，不回退旧文件写入路径。
