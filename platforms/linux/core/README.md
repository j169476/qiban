# Ubuntu 24.04 合成会话与 PostgreSQL

第二阶段在单个本机 Ubuntu 24.04 容器中运行后台、OpenSSH 和 PostgreSQL 16。三个进程共用 2 CPU、4 GiB 的 cgroup 预算。PostgreSQL 有独立持久卷，仅接受 Unix socket 的本机 peer 认证。应用以非超级用户 `qiban` 访问 `qiban_synthetic`。每个环境分别生成客户端密钥、主机密钥和共享存储加密密钥。

## 从零复跑

在活动仓库根目录的 PowerShell 中执行。构建会准备基础状态镜像，然后构建合成会话镜像。默认实例名为 `qiban-linux-core-round2`，SSH 端口为 22452。以下命令使用独立名称和端口，便于复核时自行重启。

```powershell
$core = 'platforms/linux/core/core_lab.py'
python -B $core build
python -B $core --name qiban-linux-core-review up --port 22453
python -B $core --name qiban-linux-core-review status
python -B $core --name qiban-linux-core-review accept
```

`status` 使用 Windows OpenSSH、测试专用私钥和固定的主机公钥，普通 Windows 终端可读。`up` 输出连接文件路径所需的信息，配置保存在 `.dev/linux-status/<实例名>/endpoint.json`。本机 Docker 控制台可用下面的命令独立核对主机指纹。

```powershell
docker exec qiban-linux-core-review ssh-keygen -lf /var/lib/qiban-hostkey/ssh_host_ed25519_key.pub
python -B $core --name qiban-linux-core-review database
python -B $core --name qiban-linux-core-review counters
```

完整验收在所选实例中改变合成会话授权、写入合成消息并重启容器一次。完成后撤销这一会话的授权。报告写入 `.dev/linux-status/<实例名>/core-acceptance.json`。同一实例可再次执行，断言以累计计数的本次增量为准。验收时应由一次运行独占该实例的消息投放与授权变更。

## 实际业务路径

本机控制器将内置合成输入写入 PostgreSQL 队列。后台进程独立领取，调用共享的 `events.ingest` 和 `Companion.handle`。核心构建会话上下文，校验确定性模型的结构化输出，并通过 `Companion.dispatch` 记下动作与结果。适配器使用共享的 `ReplyAuthorizations` 校验入站时固定的授权版本，在最终投递时持有与撤权相同的锁。合成收件端通过独立数据库连接提交收件记录。

合成适配器借用 QQ 私聊的权限结构，只接受内置的合成账号和收件对象。状态快照中的 QQ、微信、B 站和会客室均显示未接入。模型为确定性测试实现，历史检索使用共享核心的词法回退。

验收逐条核对以下事实。

1. 成功回复的正文摘要、收件对象、收件端回执能和核心动作账本对应。相同消息重放后，模型调用和收件记录均未增加。
2. 已经进入模型处理的消息，在处理期间撤权后产生零投递。撤权后新来的消息止于入站校验。重新授权后，旧消息携带的旧版本继续失效。
3. 收件端已经提交但回执丢失时，核心保留 `uncertain`。重新启动后重放，收件端仍只有一条记录。
4. 另一条消息在收件端已提交、核心仍为 `dispatching` 时重启。后台核对旧代次动作和同版本原消息，直接完成原工作项。动作保留不确定结果，模型判断和收件次数都应保持为一。

SSH v1 的 `progress` 统计文档索引任务。第二阶段的 `runtime.status` 另外提供可选的 `synthetic_dialogue`，其中 `backend` 为 `postgresql`，`mode` 为 `synthetic`。五项累计计数依次为 `confirmed`（已确认）、`uncertain`（回执不明）、`submissions`（收件端实际提交）、`denied`（被拒输入）和 `replays`（识别为重复的输入）。

## 逐步核对与生命周期

```powershell
python -B $core --name qiban-linux-core-review grant
$sent = python -B $core --name qiban-linux-core-review send confirm | ConvertFrom-Json
python -B $core --name qiban-linux-core-review inspect $sent.message
python -B $core --name qiban-linux-core-review replay $sent.message
python -B $core --name qiban-linux-core-review revoke
python -B $core --name qiban-linux-core-review restart
python -B $core --name qiban-linux-core-review stop
```

`send`、`grant`、`revoke`、`inspect` 和生命周期操作通过本机 Docker 控制台执行。SSH 连接只允许协商和读取状态。停止容器会保留三个命名卷。再次 `up` 可核对原安装、主机指纹与持久任务账本。第一阶段的联调环境继续使用自己的容器、凭据和数据卷。
