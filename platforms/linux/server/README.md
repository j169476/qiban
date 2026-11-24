# Ubuntu 24.04 后台与只读 SSH 状态

本地验收使用 Docker Desktop 的 Linux 引擎。Ubuntu 容器内的 `qiban` 服务用户负责持久任务和状态采样。另一个普通账号 `qiban-status` 通过 OpenSSH 固定命令访问只读 Unix socket。仅向 Windows 主机的 IPv4 回环地址发布 SSH 端口。关闭管理窗口或 SSH 会话后，工作进程继续运行。

## 建立独立验收环境

在栖伴活动仓库根目录执行下列 PowerShell 命令。主机需要 Python 3.11 或更高版本、OpenSSH 客户端和 Linux 容器模式的 Docker。首次构建需要获取 Ubuntu 24.04 镜像与 Ubuntu 软件包。默认环境名为 `qiban-linux-round1`、端口为 22441。以下命令建立单独的数据卷和主机密钥，便于与正在进行的界面联调并行。

```powershell
$lab = 'platforms/linux/server/lab.py'
python -B $lab build
python -B $lab --name qiban-linux-check up --port 22442
python -B $lab --name qiban-linux-check status
python -B $lab --name qiban-linux-check accept
```

`up` 创建或启动同名环境。容器最多可用 2 个 CPU 等量的调度时间、4 GiB 内存。报告从容器内读取 cgroup 限制和 Ubuntu 版本，主机和 WSL2 内核另计。容器的两个命名卷分别保存任务库与 SSH 主机密钥。已存在的容器使用旧镜像时，选择另一个环境名和端口验证新构建。

`accept` 在所选环境增加两件合成任务并重启一次该容器。验收期间请让这个环境独占入队，方便对照最后完成的任务 ID。报告保存在 `.dev/linux-status/<环境名>/acceptance.json`，包括断线前后和重启后的状态、索引回读结果、拒绝用例与短时资源采样。

## 普通权限入口与主机指纹

环境建立后，`status` 只使用 OpenSSH 和项目专用的客户端凭据，普通 Windows 终端即可运行。启动与重启容器、验证服务端索引或投放任务需要本机 Docker 管理权限。GUI 从 `.dev/linux-status/<环境名>/endpoint.json` 读取主机、端口、账号、测试私钥文件位置、固定的 `known_hosts` 文件位置与 SHA-256 指纹。客户端私钥仅保存在主机上，容器只挂载它的公钥。

首次启动时，脚本从本机 Docker 控制台读取主机公钥并写入专用 `known_hosts`。可以用下面两条命令核对容器主机公钥和本地记录的 SHA-256 指纹，并与 `endpoint.json` 中的 `fingerprint` 对照。Windows 窗口显示密钥匹配状态，OpenSSH 在每次连接时核验固定的完整公钥。

```powershell
docker exec qiban-linux-check ssh-keygen -lf /var/lib/qiban-hostkey/ssh_host_ed25519_key.pub
ssh-keygen -lf .dev/linux-status/qiban-linux-check/known_hosts
```

## 任务与生命周期

```powershell
python -B $lab --name qiban-linux-check enqueue --delay 15
# 关闭管理连接，等待到期后重连。把下面的参数换成 enqueue 输出的 ID。
python -B $lab --name qiban-linux-check verify <任务ID>
python -B $lab --name qiban-linux-check restart
python -B $lab --name qiban-linux-check stop
python -B $lab --name qiban-linux-check up --port 22442
```

任务输入为内置的虚构文档。后台直接调用共享核心 `companion_core.passages.split` 和 `valid` 分段、核对边界，再将各段写入此验收环境专用的 SQLite FTS5 索引。索引与完成状态在同一事务提交。工作进程重新启动后可重新领取中断的任务。`verify` 逐段比对原文、偏移及整篇 SHA-256，并用独立的全文查询核对实际命中。使用共享 PostgreSQL 存储、迁移、加密和授权的合成会话见[第二阶段](../core/README.md)。

## SSH 协议 v1

专用账号可运行固定命令 `qiban-status`。协商和后续状态请求共用同一个 SSH 会话。每行一个 UTF-8 JSON 对象，含换行最多 65,536 字节。请求 ID 为 32 位小写十六进制，回包携带相同 ID。重新连接后需要重新协商。

```jsonl
{"action":"management","operation":"protocol.hello","arguments":{"versions":[1]},"request_id":"00000000000000000000000000000001"}
{"action":"management","operation":"runtime.status","arguments":{},"request_id":"00000000000000000000000000000002"}
```

握手成功后 `result.operations` 仅包含 `runtime.status`。状态快照中的两个标识均为 32 位小写十六进制。`installation_id` 跟随数据卷持久保存，`runtime_instance` 在每次后台启动时重新生成。`generated_at` 为 UTC 采样时间，`heartbeat_count` 为当前进程的循环次数。`progress` 分别统计 queued、running、completed、failed 任务数，`last_completed_id` 初始为 null。每个平台分别提供 available、status、reason。客户端断线后将上次快照标为过期。

拒绝回包形如 `{"request_id":"…","ok":false,"error":"operation_not_allowed","message":"…"}`。验收覆盖未握手、协议不兼容、未开放操作、非固定 SSH 命令、未登记客户端密钥和主机密钥不一致。
