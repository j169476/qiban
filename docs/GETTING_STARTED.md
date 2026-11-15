# 安装与首次启动（Windows 独立应用）

栖伴当前以 Windows 独立应用的形式使用：装好或打开 `Qiban.exe`，双击后就是它自己的窗口，管理室、本机会客室和用户手册都在窗口内。**不要求预装 Python、Node.js、PostgreSQL 或编译器**，也不需要另开浏览器、终端或其他人参与的管理聊天。

版本口径：项目与插件版本 `0.9.4`，本机安装器版本 `0.9.4.177`。本轮是**实际的本机完整应用构建**：完整程序位于 `dist/Qiban-0.9.4/Qiban.exe`，安装器位于 `dist/Qiban-0.9.4-Installer/Qiban-Setup-0.9.4.177.exe`；两者都是本机本地输出，不上传仓库、也不等于公开附件。程序与安装器未做代码签名，签名与公开资产发布都未完成，**本轮不提供安装包下载地址**，也没有创建 GitHub Release。QQ 客户端运行时需要你自己已核验的本机资源，本项目不随包公开分发，也不会自动获取。

## 1. 安装或打开应用

- 已经装过：直接双击程序目录里的 `Qiban.exe`，或使用开始菜单里的“栖伴”。
- 全新安装：运行按当前用户安装的安装器 `Qiban-Setup-0.9.4.177.exe`（本机完整应用构建产出的安装器，仅本地输出，不由本项目公开提供下载）。默认把程序装到 Windows 用户资料目录下的 `Programs\Qiban`，个人资料放在 Windows 用户资料目录下的 `Qiban\Profile`。
- 同一个安装包可以重复运行，用于修复本构建里损坏或被删掉的程序与资源文件。更新到另一个构建时，安装器会按下文的配套升级流程处理。
- 栖伴正在运行时，安装和卸载都要求先在窗口里正常退出，不要强制结束进程。
- 卸载只删除安装器登记的程序文件和快捷方式；个人资料、备份与凭据原样保留。

卸载入口已统一：程序目录中的 `uninstall.exe`、开始菜单的“卸载栖伴”、Windows 应用列表，都会进入卸载准备页。应用内也可从顶部“应用设置 → 卸载栖伴”进入，当前后台会先正常关闭。

准备页沿用应用布局，可保存本机加密备份，或用独立恢复口令导出便携备份（`.ccbackup` 与同名 `.cckey` 成对保管）。点击备份会自动准备服务；备份完成后，再点“继续卸载”并确认。也可明确选择“跳过备份”。取消或失败不会被标记为备份成功，单纯完成备份也不会自动卸载。自己导出的便携备份请保存在程序目录之外。

直接解压使用的目录没有登记卸载程序，`uninstall.exe` 仍可用于备份，随后正常退出并手动删除程序文件夹即可。卸载准备只打开数据库和备份所需服务，不启动模型或平台任务。

## 2. 首次启动：建立资料与数据库

首次启动由应用自己完成初始化，不需要任何命令：

- 建立本安装专属的角色身份、独立密钥和受限数据库运行账号，并把加密 PostgreSQL 建成本安装自己的库（首次建库）。
- 密钥与初始化密码存入 Windows 凭据库；密码只经仅本机当前用户可访问的命名管道传递，配置和日志不记录密码。
- 用当前 Windows 登录用户 SID 限制资料目录访问，并拒绝同一资料被并发准备；初始化中断后可重新启动继续。
- 凭据缺失或数据库异常时明确失败并给出可操作提示，**不会重新生成密钥**，也不会删库重来。
- 角色名称、人物设定、表达风格、基础记忆和头像都是空的，等待你自己填写；已有资料不会被初始化覆盖。

初始化完成后打开的就是管理室。关窗收至托盘，再次打开回到同一窗口；重复启动只会激活同一实例，不会重复启动后台或数据库。

## 3. 本机管理员与内置界面

- 默认管理员是**当前 Windows 登录用户**，打开应用即可管理，不需要再输一遍栖伴密码。
- 设置页的“应用密码锁（可选）”由你主动开启：锁定后要解锁才能进入管理页或会客室；会客室已收到的消息仍按模型额度继续生成回复，解锁后可查看。
- 管理室负责人物卡与基础记忆、角色与记忆、任务、额度与用量、平台与会话、设置；内置《用户手册》可随时打开对照。
- 窗口内左侧导航切换分区，中间是当前分区的操作区，右侧显示该分区的说明与限制；应用设置集中在顶部，保存由一个统一入口提交，结果与错误在对应控件附近反馈。
- 本机会客室与管理室共用同一本机管理身份，在同一个窗口里进入和返回。
- 角色与模型不获得管理员权限；改密码、锁定界面、恢复备份都不等于允许平台自动活动。

## 4. 按需登录平台，默认停用

所有平台账号都由你在应用内按需登录和核验，**新安装不会自动启动任何平台**：

| 平台 | 在应用内要做的事 | 本机还需准备的资源 |
| --- | --- | --- |
| B 站 | 自行登录账号，按平台策略与额度运行内容与私信工作流 | 无额外二进制；账号与授权自备 |
| QQ | 准备独立测试账号，在应用内启动原生连接器、扫码登录并核验账号 | 自行准备与源码锁定版本一致的本机 QQ 运行资源 |
| 微信 | 登记账号、指定加密数据库收件位置，核验后仅做受限文本发送 | 本机已构建并核验的原生发送桥接；受精确客户端版本与文件指纹限制 |

- 平台卡片里不会因为登录就进入运行；启动、登录、授权、资料读取是分开的控制项。
- 微信与 QQ 默认不回复任何会话。可勾选授权对象，或在看过提示后授权相应账号的所有好友或群。私聊额度与群参与额度各自独立。
- 首次接入时自动运行默认关闭；之后按你自己的偏好设置，应用会在下次打开时先检查账号、模型等就绪条件，再决定是否启动。你在本次应用会话中主动停止的平台，不会因重连自动恢复。
- 发送失败或结果不明时先看状态与动作记录，不要靠反复重试判断是否成功。

## 5. 更新、回退与退出

- 跨构建配套升级由**已安装的那一版自己的维护后台**准备：先保全资料、切换资料代次、保留一份可回退的旧程序，只有准备成功才覆盖文件。176 及更早的安装包没有这个维护入口，本安装包会明确拒绝覆盖，请改用与目录内版本配套的维护包。
- 开始菜单里的“栖伴升级回退”指向保留下来的旧版程序，升级后第一次启动会校验这次升级；校验没通过就先退出，用它回到升级前的版本，不要手动删程序目录。
- 没有联网更新源，应用不会自动下载或更新；也不会因为网络不可用而擅自降级或改写程序目录。
- 正常退出会停止栖伴自己的后台与数据库，不影响微信、QQ 客户端或其他程序。资料在运行中提交，退出时不承诺回调一定有机会完成；结果不明的发送与已占用额度会保留，禁止自动补发。

## 6. 从源代码构建（开发者，不是普通用户操作）

旧版入口已经退役：`python -m qiban` 与仓库里的插件启动器只输出退役提示并以非零码退出；`scripts/bootstrap.py` 仍按旧方式服务于已退役的插件开发环境。**普通用户不要运行这些命令，也不要再在宿主里注册旧的插件入口。** 下面的命令只用于自己构建独立应用，取自 `scripts/desktop_probe/README.md`，并已删去与旧入口相关的部分。

```powershell
# 1) 内置存储后台（仅针对 CPython 3.11 Windows x64，使用固定锁文件）
python -m venv .dev/storage-build-python
.dev/storage-build-python/Scripts/python.exe -m pip install --require-hashes -r scripts/desktop_probe/storage-requirements-lock.txt
.dev/storage-build-python/Scripts/python.exe scripts/desktop_probe/build_storage.py --output .dev/storage-bundle-new --work .dev/storage-build-new

# 2) 内嵌 PostgreSQL 资源（先替换示例路径）
python scripts/desktop_probe/build_postgres.py --source "C:\BuildTools\postgresql-18.6" --output .dev/postgres-resources-new

# 3) 桌面外壳（托管模式；需要 .NET 桌面与 WebView2 构建环境）
./scripts/desktop_probe/build.ps1 -ManagedApplication -SubscriptionValidation -OutputDirectory .dev/managed-desktop-new

# 4) 组装完整应用目录（可再加 --qq-runtime／--wechat-runtime／--speech）
python scripts/desktop_probe/assemble_application.py --desktop .dev/managed-desktop-new --storage .dev/storage-bundle-new --postgres .dev/postgres-resources-new --output .dev/managed-app-new

# 5) 编译安装包（先替换为实际的 Inno Setup 编译器路径）
python scripts/desktop_probe/installer/build_installer.py --app .dev/managed-app-new --output-dir .dev/installer-new --iscc "C:\BuildTools\InnoSetup\ISCC.exe" --version 0.9.4.177
```

构建要点：

- 存储后台与 PostgreSQL 资源都必须给出全新的 output/work 目录，构建会核对环境版本与锁文件；不会覆盖正在使用的旧包。
- 如果 PostgreSQL 分发缺少所需的 Visual C++ 运行库，构建工具会列出缺失模块。使用 `--vc-runtime` 指向匹配的 Microsoft 可再发行文件，并用 `--vc-license` 提供随包许可。安装器可加 `--check-only` 先核对应用载荷。
- 组装后的目录必须整体保留（含 `resources` 与许可文件），只复制 `Qiban.exe` 不能运行；`Qiban.exe` 是外壳程序同字节改名，构建记录把程序、外壳源码归档与组件资源绑在同一个构建身份上。
- 微信原生发送桥接与 QQ 桥接不在这条流水线里替你做：分别按 `src/companion_wechat/native/README.md` 和 `scripts/qq_native_probe/README.md` 自行准备并核验本机资源。
- 第 3 步必须同时带 `-ManagedApplication -SubscriptionValidation`。历史内部开关名 `SubscriptionValidation` 对应应用独立走 ChatGPT 授权（0.9.4 使用 `gpt-6.1-terra` 与 low 推理档位），不需要 API Key，也不经过 Codex 聊天窗口，但仍要求本机有可用的 Codex 可执行文件；QQ 与微信的回复模型接线复用应用的本地模型组件 `DesktopModel`。
- 本轮完整程序位于 `dist/Qiban-0.9.4/Qiban.exe`，安装器位于 `dist/Qiban-0.9.4-Installer/Qiban-Setup-0.9.4.177.exe`；两者都只在本机保留，不上传仓库，也不创建 GitHub Release。
- 公开同步以 `scripts/prepare_release.py` 生成的无历史审查副本为准；构建产物、测试输出与证据留在本机，不当作发布附件。

## 7. 旧版文档如何阅读

旧浏览器管理页及其过期说明已移除。当前操作以应用内页面和内置用户手册为准；版本变更见 [版本说明](RELEASE_NOTES.md)。
