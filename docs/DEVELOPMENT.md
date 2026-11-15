# 开发、测试与发布

## 当前重构工作区

独立 EXE 重构以 0.9.4（00d3367a）为基线，使用本仓库作为唯一开发与构建源码；当前分支为 main。父目录中的旧开发文件保留作历史核对，不参与新构建。日常安装与本仓库分离。

所有命令从本仓库根目录执行；本仓库自己的 .venv 用于常规开发，.dev 存放已忽略的测试证据和临时资源，dist 存放生成产物。临时借用其他虚拟环境时，必须明确设置本仓库 src 为导入路径并核查实际加载位置，避免误测旧版。

仓库仅保留当前使用、开发和许可说明。阶段交接、重构计划及旧网页文档已清理；历史上已提交的内容可通过 Git 历史查看。

常规开发安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints-windows.txt -e ".[dev,service,qq]"
.\.venv\Scripts\python.exe -m ruff check src tests tests_core tests_postgres tests_wechat scripts
.\.venv\Scripts\python.exe -m pytest -q tests tests_core tests_wechat
.\.venv\Scripts\python.exe scripts/check_release.py
```

SQLCipher 测试需要 `requirements/wechat-db.txt`，缺少可选依赖的测试会跳过。PostgreSQL 回归使用独立测试实例，不能指向个人运行数据库：

```powershell
.\.venv\Scripts\python.exe scripts/ci_postgres.py start
$env:COMPANION_TEST_DATABASE = '.dev/ci-postgres/connection.json'
$env:COMPANION_REQUIRE_POSTGRES = '1'
.\.venv\Scripts\python.exe -m pytest -q tests_postgres
.\.venv\Scripts\python.exe scripts/ci_postgres.py stop
```

`ci_postgres.py` 可通过 `--bin` 使用已准备的 PostgreSQL 二进制目录。测试在独立 schema 中运行；结束后停止测试实例。CI 配置位于 `.github/workflows/tests.yml`。微信原生模拟测试运行 `scripts/test_wechat_native.cmd`，不加载真实客户端。

检索评测的合成夹具位于 `tests_core/fixtures/retrieval/` 和 `tests/fixtures/`，评测脚本包括 `evaluate_algorithm_routing.py`、`evaluate_postgres_governance.py`。这些数据不是基础记忆，也不会由安装器导入用户资料。

## 当前应用界面结构

独立应用不做网页管理页：左侧导航切换分区，中间是当前分区的操作区，右侧显示该分区的说明与限制，应用设置集中在顶部。保存由一个统一入口提交，结果与错误在对应控件附近反馈，不再分散到独立保存页。

## 独立应用构建与产物

正式构建必须同时带托管应用与应用自己的授权两个开关：

```powershell
./scripts/desktop_probe/build.ps1 -ManagedApplication -SubscriptionValidation -OutputDirectory .dev/managed-desktop-new
```

历史内部开关名 `SubscriptionValidation` 对应应用独立走 ChatGPT 授权：0.9.4 使用 `gpt-6.1-terra` 与 low 推理档位，不需要 API Key，也不经过 Codex 聊天窗口，但仍要求本机有可用的 Codex 可执行文件。QQ 与微信的回复模型接线复用应用的本地模型组件 `DesktopModel`；本轮只补齐接线，没有重新实测真实平台或在线模型。

本轮实际产出的完整程序位于 `dist/Qiban-0.9.4/Qiban.exe`，安装器位于 `dist/Qiban-0.9.4-Installer/Qiban-Setup-0.9.4.177.exe`。`dist` 只在本机保留：不上传仓库、不当作发布附件，也不创建 GitHub Release；安装器与完整程序都未做代码签名。

## 无历史发布副本

```powershell
.\.venv\Scripts\python.exe scripts/prepare_release.py
.\.venv\Scripts\python.exe scripts/prepare_release.py --output .dev/releases/first-test
```

可加 `--deny-file .dev/private-release-markers.txt`，提供每行一个的个人标识作额外扫描；该文件和匹配文本不会输出到报告或发布包。扫描只覆盖已知模式与提供的标识，不是任意秘密检测器，仍需查看文件清单。

导出器只选取源码、测试、插件、依赖和公开文档，拒绝未审查的二进制和链接，排除本机运行资料、头像与个人文档。它强制两个默认人物卡为空，输出：

- `repository/`：不含旧 `.git` 的发布源码树；
- `source.zip`：相同文件的源码压缩包；
- `audit.json`：文件路径、长度、SHA-256 与压缩包摘要。

已有目标目录不会被覆盖。需要重新导出时使用新目录，保留此前审查结果。导出器不提交、不推送、不创建 GitHub 仓库。

**曾提交个人资料的开发历史不能直接发布。** 从审查后的 `repository/` 建立新的 Git 初始提交，再推送到确认的目标仓库；不要强推覆盖已有远程历史。确认第三方许可和自有代码许可证后，再决定对外分发方式。

在发布副本中构建，避免开发目录里旧的构建元数据影响打包：

```powershell
python -m pip wheel --no-deps --no-build-isolation . --wheel-dir ..\wheels
python -m pip wheel --no-deps --no-build-isolation packages/companion-core --wheel-dir ..\wheels
```

用 `scripts/check_core_package.py` 验证核心 wheel 的平台隔离性。QQ、微信独立包可分别从 `packages/companion-qq`、`packages/companion-wechat` 构建。核对 wheel 内容不包含运行库或角色素材，再整理发布附件。

## 源码与生成文件

桌面界面位于 `scripts/desktop_probe/Assets/`，由桌面项目直接嵌入；旧浏览器模板、脚本和启动器已经移除。`companion_service` 中桌面仍引用的任务、会客室与恢复逻辑继续保留，兼容 API 的权限回归也继续保留。旧 HTTP 首页及手册返回退役提示，不再提供浏览器界面。

构建输出、测试临时目录、Python 缓存、包元数据及阶段记录均不提交。`dist/` 中正在运行的完整应用不应在清理时删除；日常运行数据、数据库、凭据和开发工具环境也不属于构建垃圾。

发布审计按 SHA-256 校验桌面插画与必要上游资源，其他未审查二进制仍拒绝。更新素材后先验收再更新审计摘要。
