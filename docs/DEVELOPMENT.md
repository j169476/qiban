# 开发、测试与发布

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
