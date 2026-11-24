# 栖伴 Linux 后台的 Windows 远程控制台

这个 Windows x64 客户端通过 SSH 查看 Ubuntu 24.04 栖伴后台的状态、任务进度和平台可用性。它独立于栖伴 Windows 个人版运行。Linux 服务持续处理任务，关闭控制台只会断开管理连接。

## 从源码启动

需要 Windows x64、Windows PowerShell 5.1 和带 C# 编译器的 .NET Framework 4.x。建立 SSH 连接还需要 Windows 可选功能 **OpenSSH 客户端**。在仓库根目录运行以下命令，也可以在文件资源管理器中双击 `platforms\windows-remote\start.cmd`。

```powershell
.\platforms\windows-remote\start.cmd
```

首次启动会在本机构建 `platforms\windows-remote\build\Qiban.Remote.exe`，随后打开独立窗口。全新检出时窗口处于「已断开」，可先打开查看界面。已有默认本机测试环境时，启动器会预填该环境的连接信息，点击「连接」才会发起 SSH 请求。

更新源码后，关闭旧窗口并显式重新构建，再运行上述启动命令。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\platforms\windows-remote\build.ps1
```

## 连接 Linux 后台

如果已有兼容 SSH v1 的栖伴后台，点击「载入连接文件…」选择受信任的 `endpoint.json`，再点击「连接」。也可在窗口手动填写主机、端口、客户端身份密钥和经服务器控制台核对的 OpenSSH `known_hosts` 文件。

要从零建立本机 Ubuntu 测试后台，先准备 Python 3.11 或更高版本以及运行 Linux 容器的 Docker。以下命令由 Linux 侧生成专用测试密钥、固定的主机公钥文件和连接配置，并启动默认的回环测试实例。

```powershell
python -B platforms/linux/server/lab.py build
python -B platforms/linux/server/lab.py up
.\platforms\windows-remote\start.cmd
```

默认实例名为 `qiban-linux-round1`。使用其他实例时，将它实际生成的连接文件传给启动器。例如，按 PostgreSQL 合成会话说明建立 `qiban-linux-core-review` 后，可运行下列命令。该实例另行显示「测试会话」的五项累计数。[Ubuntu 后台与 SSH 协议](../linux/server/README.md) · [PostgreSQL 合成会话](../linux/core/README.md)

```powershell
.\platforms\windows-remote\start.cmd --profile ".dev\linux-status\qiban-linux-core-review\endpoint.json"
```

连接文件的字段为 `host`、数字 `port`、`user`（固定为 `qiban-status`）、`identity_file` 和 `known_hosts`。文件路径可以是 Windows 本机绝对路径，也可以相对于连接文件所在目录。客户端通过系统 OpenSSH 校验完整的主机公钥，使用非默认端口时主机记录采用 `[主机]:端口` 格式。测试环境将每台机器自己的配置、SSH 文件和验收记录保存在仓库根目录的 `.dev\` 中。

连接成功后，窗口显示服务端采样时间和运行实例。手动断开会保留最后一次采样并标记为离线。临时网络故障会触发自动重连。主机公钥或客户端身份验证失败时，窗口显示错误并等待处理。

## 验证

构建客户端后，在 Windows 交互式桌面的 PowerShell 中运行显示层检查。默认使用合成数据；指定 `-EndpointFile` 可额外用正式连接配置核对采样、断开和重连。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\platforms\windows-remote\Display\verify-display.ps1
```

要复验完整链路，先运行上文的 Ubuntu 镜像构建命令，然后执行下列命令。验证工具默认使用独立实例 `qiban-linux-remote` 和端口 22442；可用 `--name`、`--port` 指定另一组可用的名字和端口。

```powershell
python -B platforms/windows-remote/verify.py
```

验收通过正常窗体连接、主动断开和重连，在 SSH 意外中断后验证自动恢复，并核对错配的主机公钥会被拒绝。客户端断开期间，Ubuntu 侧测试工装投递一项合成文档索引任务并检查检索结果。每次验收的 `result.json` 和 `index.json` 写入 `.dev/windows-remote-check/<运行目录>/`；显示层检查的报告和截图默认位于 `.dev/remote-display-<运行标识>/`。
