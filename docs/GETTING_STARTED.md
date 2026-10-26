# 安装与首次启动

这是源码测试版，当前本机服务安装器面向 Windows。准备 Python 3.11 或更新版本、Git、可用的 Codex 登录，以及来源可信的 PostgreSQL Windows 二进制目录（含 `bin`、`lib`、`share`）。微信发送桥接另外需要 MSVC x64 构建工具。请在新的工作目录安装，不复用他人的运行库或凭据。

## 1. 创建源码环境

在仓库根目录运行：

```powershell
python scripts/bootstrap.py --register
```

该命令创建 `.venv`、安装开发与本地服务依赖、生成本机插件运行路径，并初始化空白角色资料。`--register` 只登记本地插件目录，不代替用户在 Codex 中安装插件和审阅钩子。它不启动平台角色，也不替你登录平台。

按需要补装 QQ 依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -c constraints-windows.txt -e ".[qq]"
```

微信只读数据库运行时独立安装，见 [微信模块](wechat-module.md)。不要把生成的 `runtime.json`、`.venv` 或 `bili-assistant-data` 提交到 Git。

## 2. 安装共享本地服务

将下面的 PostgreSQL 路径替换为自己的二进制目录：

```powershell
.\.venv\Scripts\python.exe -m companion_service install --project . --postgres-bundle "C:\Tools\pgsql"
.\.venv\Scripts\python.exe -m companion_service install-status --project .
```

安装器从上一步初始化的资料迁移到专用 PostgreSQL，建立受限数据库账号，将凭据保存到系统凭证库。管理员和角色 HTTP 端口默认分别为 `45180`、`45181`，只监听回环地址。安装失败时先检查 `install-status`，不要删除安装记录后重复覆盖。

需要手工前台运行服务时：

```powershell
.\.venv\Scripts\python.exe -m companion_service serve --project .
```

正常插件入口也会管理服务生命周期。安装、服务启动、重启和解除归档都不意味着允许平台自动活动。

## 3. 设置管理员与人物卡

在 Codex 中安装本仓库登记的插件，选定独立的后台管理聊天，选择 `@qiban` 插件或输入 `$qiban` 进入统一入口，再绑定管理聊天。首次设置通过 `service_setup` 返回的一次性本机页面完成，再通过 `management_open` 打开管理页面。聊天命令必须使用真实宿主提供的身份，普通终端不要自行设置 `CODEX_THREAD_ID`。

管理网页填写人物卡和基础资料；新安装没有预置人物关系或外观。详见 [配置说明](CONFIGURATION.md)。按需登录 B 站、接入 QQ 或登记微信，然后逐条授予会话回复权限。

## 4. 明确启动所需模块

在实际控制角色的 Codex 聊天中指定平台，例如“启动 QQ 模块”。对应入口为 `qq_start/stop/status`、`wechat_start/stop/status`、`bili_start/stop/status`，本机会客室使用 `local_assistant_*`。含糊的启动指令应先确认平台。

建议先使用自己控制的测试账号做私聊验收，再单独授权群聊。发送失败或结果不明时查看状态和动作记录，不要通过反复重试来判断是否成功。实际验收和长时运行由部署者完成。

## v0.9.1 升级与恢复

已安装旧版时保留原数据目录和本机服务配置，运行 bootstrap 后安装 `qiban@xiaolongniang-local`，确认新入口正常再移除旧插件。内部 bili_assistant 模块与旧数据目录用于兼容，不重新初始化已迁移数据库。

网页的“平台与会话”也可启动已配置平台，任务归属已绑定管理聊天；归档管理聊天会停止这些任务。关闭管理页和关闭聊天视图保持运行。退出 Codex 后角色停止，再打开应用需在管理聊天明确请求“重启服务”，或运行 `.\.venv\Scripts\python.exe -m qiban restart --project .`，然后单独选择要启动的平台。网页“系统设置”可请求打开 Codex 工作区，不会自动发消息。

Windows 实机验收确认，本地服务、监护与数据库可能随 Codex 一起结束；此时网页显示连接中断，无法继续管理配置。关键记录在运行中提交，不承诺退出回调有机会完成。下次启动先检查中断状态，取消未发送动作，保留结果不明的发送记录和已占用额度，禁止自动补发；服务与平台均需明确恢复。网页断线不能直接等同已确认宿主退出。
