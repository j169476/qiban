# 栖伴 Linux 后台

两个阶段均可从仓库根目录构建，在本机 Ubuntu 24.04 容器中验证。Windows 管理窗口通过只读 SSH 查看服务状态。

| 入口 | 可以验证的行为 |
| --- | --- |
| [后台服务与文档索引](server/README.md) | 客户端断线期间完成 SQLite FTS5 索引，重连后核对任务与命中结果。 |
| [共享核心与 PostgreSQL](core/README.md) | 在前一阶段的镜像上运行合成会话，核对权限撤销、重复消息、投递及回执不明时的重启恢复。 |
| [Windows 只读管理客户端](../windows-remote/README.md) | 打开普通权限窗口，从本机生成的 `endpoint.json` 连接，校验固定主机密钥并读取状态。 |

### 构建所需源码

保留仓库相对路径，Docker 构建上下文取仓库根目录。每个阶段的 `Dockerfile.dockerignore` 列出该镜像可以读取的源文件。

- 索引阶段使用 `server/` 中的 Python 源码、SSH 配置及构建文件。共享核心仅需 `src/companion_core/` 下的 `__init__.py`、`passages.py` 和 `index_cache.py`。容器运行时使用 Ubuntu 的 Python、SQLite FTS5 和 OpenSSH。
- 合成会话阶段还需要 `core/`、`src/companion_core/` 顶层的 Python 模块和 JSON 资源，以及 `src/companion_core/sql/*.sql` 迁移。基础镜像提供索引服务和 SSH 网关；第二阶段安装 PostgreSQL 16 及 [Python 依赖](core/requirements.txt)。合成模型与本机收件端随源码提供，历史检索使用词法回退。
- 主机端使用 Python 3.11+、OpenSSH 客户端和 Docker Linux 引擎。构建和验收脚本随各自阶段提供。脚本把客户端测试密钥、主机公钥记录、连接配置及验收报告写到 Git 忽略的 `.dev/linux-status/<实例名>/`。

独立验证候选源码时，可在 PowerShell 中先设 `$env:QIBAN_LINUX_IMAGE_TAG = 'candidate'`，再按阶段说明运行 `build`、`up`、`status` 和 `accept`。两个阶段共用此标签，默认值为 `local`。请使用新的实例名和回环端口，`accept` 会写入测试数据并重启所选实例。

两个阶段均按容器合计 2 CPU、4 GiB 做短时功能验收。整机 4 GiB 长期运行、真实平台连接和实际模型效果仍待验证。
