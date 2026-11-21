# Windows 开发与发布

## 构建应用

桌面程序使用 .NET 和 WebView2，存储后台使用 Windows x64 的 CPython 3.11。准备 PostgreSQL 运行组件、Inno Setup 及所需的平台运行资源后，在仓库根目录执行以下命令。示例中的工具路径需要替换成本机位置。

```powershell
python -m venv .dev/storage-python
.dev/storage-python/Scripts/python.exe -m pip install --require-hashes -r scripts/desktop_probe/storage-requirements-lock.txt
.dev/storage-python/Scripts/python.exe scripts/desktop_probe/build_storage.py --output .dev/storage --work .dev/storage-work
python scripts/desktop_probe/build_postgres.py --source "C:/BuildTools/postgresql" --output .dev/postgres
./scripts/desktop_probe/build.ps1 -ManagedApplication -SubscriptionValidation -OutputDirectory .dev/desktop
python scripts/desktop_probe/assemble_application.py --desktop .dev/desktop --storage .dev/storage/Qiban.Storage --postgres .dev/postgres --qq-runtime .dev/qq --wechat-runtime .dev/wechat --speech .dev/speech --output dist/Qiban-1.0.0
python scripts/desktop_probe/installer/build_installer.py --app dist/Qiban-1.0.0 --output-dir dist/Qiban-1.0.0-Installer --iscc "C:/BuildTools/InnoSetup/ISCC.exe"
```

组装时可用 `--qq-runtime`、`--wechat-runtime` 和 `--speech` 指定已准备的对应组件。生成的 `installer-build.json` 记录安装包大小和 SHA-256。

## 测试和公开源码

项目的 Windows 验证流程见 [GitHub Actions 配置](../.github/workflows/tests.yml)。数据库集成测试使用独立测试实例。前端浏览器检查位于 `scripts/desktop_probe`。

```powershell
python scripts/prepare_release.py
python scripts/prepare_release.py --output .dev/releases/windows-1.0.0
```

发布工具导出经审查的源码、使用文档、许可与美术素材。公开仓库保留当前用户指南和模型参考表。原始测试记录、交接文档和完整安装包存放在本机。

## 隐藏的单次回复计时

详细计时默认关闭，没有前端按钮。普通回复保留原有摘要记录；开发者显式调用后台后，才采集指定平台的下一次回复。一次只允许一个采集请求，10 分钟没有触发就失效；完成、失败或取消的回复均消耗这次请求，不会继续采集后续回复。

应用后台运行时，在应用目录执行（开发版与安装包共用同一入口）：

```powershell
$profileConfig = Join-Path $env:LOCALAPPDATA 'Qiban\Profile\data\installation\storage-config.json'
.\resources\storage\Qiban.Storage.exe --config $profileConfig --developer-reply-timing arm --timing-platform qq
.\resources\storage\Qiban.Storage.exe --config $profileConfig --developer-reply-timing report
.\resources\storage\Qiban.Storage.exe --config $profileConfig --developer-reply-timing cancel
```

`arm` 只开启计时，不启动平台、不发送消息。平台可选 `qq`、`wechat`、`bilibili`、`local`。源码调试时可用 `python scripts/desktop_probe/storage_worker.py` 替代后台程序路径。入口沿用当前 Windows 用户的凭据库和个人资料代次，不启动第二个数据库或模型后台。

报告为 JSON，最多保留最近一次报告、256 个计时区间，不含消息正文、账号、模型输入输出或异常原文。`analysis` 给出总耗时、各主阶段占比、最慢主阶段和未单独归类的时间。`spans` 中 `parent` 表示父阶段，`start_ms` 为相对开始时间，`duration_ms` 含子阶段，`self_ms` 扣除子阶段区间的并集；父子耗时不能直接相加，平行任务的总工作时间也不能当作墙钟时间。超出区间上限时 `limited` 为真。

`reply.total` 从回复处理入口开始，包括会话锁等待、上下文、模型、保存、平台发送和回执，不包括消息到达处理入口之前的平台轮询、合并等待或网络传输。`model.total` 含准备、进程启动、初始化、请求与等待、结果校验及清理；未单独标记的清理等耗时体现在其 `self_ms`。`model.request_and_wait` 是应用观测到的服务往返，不能进一步推断服务器排队与实际推理各占多少。已经完成且未开启详细计时的历史回复不能回填细分耗时。
