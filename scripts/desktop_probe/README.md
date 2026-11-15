# Windows 桌面容器探针（R0.6）

这是自有 MIT 验证代码，不是最终 Qiban 安装包。验证 WinForms + WebView2 内置页面、托盘、单实例、文件对话框和后台失联显示；默认使用合成后台服务；可用显式测试配置接入独立 PostgreSQL，或数据库与加密存储后台的组合。不接入日常数据库、角色、账号或发送接口。

## 构建与运行

需要 Windows x64、.NET 10 SDK；首次构建需访问 NuGet。构建固定 WebView2 SDK 1.0.4314.35、.NET runtime 10.0.14，依赖锁定记录在 packages.lock.json。

从仓库根目录执行：

```powershell
./scripts/desktop_probe/build.ps1
& ./.dev/refactor-r0/desktop/portable/Qiban.DesktopProbe.exe --data-dir ./.dev/refactor-r0/desktop/manual-profile
```

构建缓存、输出和验证资料均位于忽略目录 .dev/refactor-r0/desktop。运行时需要已安装的 WebView2 Runtime；本次机器已有该组件。EXE 自包含 .NET，首次运行可能向临时目录解包原生库。发布时须保留 portable 目录内随附的许可证文件。

省略 --data-dir 时只使用 Windows LocalAppData/Qiban/Profile（安装包把程序装到 LocalAppData/Programs/Qiban，卸载只去程序目录，个人资料仍在该 Profile 里）；--data-dir 指定独立资料根目录，具体布局见下文。旧默认目录 LocalAppData/Qiban/DesktopProbe 不会被搬动。资料迁移尚未实现。相同数据目录的第二次启动会唤回已有窗口。关闭按钮收至托盘，页面或托盘的退出操作关闭验证应用。

## 自动检查

使用项目测试 Python 运行；evidence 目录必须不存在：

```powershell
python scripts/desktop_probe/run_smoke.py --exe .dev/refactor-r0/desktop/portable/Qiban.DesktopProbe.exe --evidence .dev/refactor-r0/desktop/smoke-new
```

监督器将整棵自有进程树置于 Windows Job 中，60 秒超时后清理；生成 result.json、supervision.json 和 preview.png。不要只看截图判断测试成功，必须检查退出码及 passed。检查共 9 项（并单独核对退出应用前的后台正常停止）：离线页面、命名管道通信、后台被终止后的失联、页面停止并重新启动、收至托盘、第二实例唤回、旧手册内置加载、外部导航阻断、两种宽度无横向溢出。

文件对话框选取成功、跨显示器 DPI、干净机器安装仍需另外验证。交互运行的后台现已使用创建时加入的 Windows Job，父进程异常退出会结束自有后台及后代。run_lifecycle.py 在独立测试安全 Job 关闭前检查退出事实；正式 PostgreSQL／Codex／QQ 的停止流程和 R1 组件管理器仍待接入。

WebView2 SDK 原始许可保留在 licenses/WebView2-SDK.txt；.NET 与其他组件许可由 build.ps1 从对应固定版本 NuGet 包复制到输出目录。自有代码 MIT 不替代这些条款。

历史探针记录已清理；当前应用构建说明见 ../../docs/DEVELOPMENT.md。

## 后台生命周期验证

新增 OwnedProcess.cs 为合成后台创建专属 Windows Job。通过 PROC_THREAD_ATTRIBUTE_JOB_LIST 在创建子进程时直接加入 Job，不依赖“先运行、后收编”。Job 句柄不继承，所有者失去句柄后系统终止其后台树；正常停止现先通过组件适配器请求退出并等待；超时或通信失败则终止自有 Job，并明确记录强制停止。Job 最终清空后才释放所有权。不会按进程名称扫描终止其他软件。

可构建到独立目录，避免覆盖正运行的旧验证包：

```powershell
./scripts/desktop_probe/build.ps1 -OutputDirectory .dev/refactor-r0/desktop/portable-owned
python scripts/desktop_probe/run_lifecycle.py --exe .dev/refactor-r0/desktop/portable-owned/Qiban.DesktopProbe.exe --evidence .dev/refactor-r0/desktop/lifecycle-new
```

生命周期检查涵盖两次正常退出、三次只终止所有者、子进程和孙进程退出、无关同名进程继续运行、空参数／空格／引号／末尾反斜线传递、不存在可执行文件启动失败，以及实际窗口后台就绪后强制终止窗口的清理。测试只创建合成进程，不连接生产服务。真实窗口用独立数据目录，测试时可能短暂显示验证窗口。

LifecycleProbe.cs 与 --lifecycle-window 是本探针的专用验收入口，不是生产接口。原有 headless 生命周期测试仍直接终止 Job；新增组件测试会区分协议请求后的正常退出与超时清理。两者均不能替代正式数据库或平台在途动作的停止验收。WebView 浏览器主进程另由 WebView2 自身随宿主退出，本次观测其退出，不声称全部渲染后代都纳入后台 Job。

## 目录分离与程序位置验证

AppPaths.cs 统一管理验证应用的目录。程序与许可证留在 EXE 所在目录，独立资料根目录下分为：

| 子目录 | 用途 |
| --- | --- |
| data | 持久资料预留；探针不接入生产数据库 |
| logs | 桌面事件 events.jsonl |
| cache/webview | WebView2 页面缓存与配置 |
| temp | 进程临时文件；TEMP/TMP 仅在本进程和派生进程内设置 |
| resources | 模型等资源预留；尚未实现下载、加载或升级 |

测试结果 result.json、supervision.json 与截图 preview.png 仍写入验证根目录，属于测试产物。普通启动不生成这些测试文件。单文件 .NET 宿主可能在 Main 运行前解包原生库，这部分不受进程进入 Main 后的 TEMP/TMP 设置控制。日志尚未增加轮转或保留期限。

```powershell
./scripts/desktop_probe/build.ps1 -OutputDirectory .dev/refactor-r0/desktop/portable-paths
python scripts/desktop_probe/run_layout.py --exe .dev/refactor-r0/desktop/portable-paths/Qiban.DesktopProbe.exe --evidence .dev/refactor-r0/desktop/layout-new
```

run_layout.py 将整个构建目录复制两份，包括所有随附许可证；从另一个空目录启动，子进程 PATH 仅保留 Windows System32。它执行三轮真实窗口检查：首次启动、同一资料目录改用另一份程序且缓存移走后重启、新资料目录启动。每轮均执行九项桌面检查并核对整应用退出时的正常停止，比较程序目录文件摘要及目录清单，确认没有写入程序或工作目录；合成资料及合成资源占位文件在第二轮保持逐字节一致。旧缓存仅移到本轮证据目录并保留，不删除任何日常资料。

测试始终显式传入资料目录。不要通过替换 LOCALAPPDATA 环境变量模拟 Windows Known Folder：本机实测系统目录解析不会随该变量改变。该测试不依赖安装器，不代表真实版本升级、数据库迁移、模型可用性或干净机器安装已通过。

## 组件状态与受控停止

ManagedComponent.cs 管理单个组件的启动、就绪、停止和失败状态；ComponentDefinition 声明组件 ID、启动／停止期限及适配器操作。ProbeService.cs 是当前唯一接入的合成适配器，校验协议版本和返回的 PID 与自有进程一致。此握手是配置／实例一致性检查，不作为正式管理端的身份认证方案。

就绪阶段设置 5 秒期限（随后进程清理耗时另计），每次通信最多 300 毫秒，未就绪时隔 100 毫秒检查同一个进程；不会为一次启动反复创建新进程。停止先请求退出，正常退出等待期限为 2 秒（强制清理耗时另计）；卡住或通信失败后清理自有 Job 并记录 forced_stop。正常退出记录 graceful_stop。周期健康检查失败后进入 failed，不自动重启；用户可以明确重新启动。

同一组件的操作串行化。停止会撤销正在等待就绪的启动和此前排队的启动；新一次明确启动才能再次运行。页面显示启动中、已连接、停止中、已停止和失败，并限制重复按钮操作。退出应用先执行停止流程再关闭窗口；宿主意外结束仍由 Windows Job 清理自有后台。

```powershell
./scripts/desktop_probe/build.ps1 -OutputDirectory .dev/refactor-r0/desktop/portable-components
python scripts/desktop_probe/run_components.py --exe .dev/refactor-r0/desktop/portable-components/Qiban.DesktopProbe.exe --evidence .dev/refactor-r0/desktop/components-new
```

十类真实子进程检查（新增停止后返回非零退出码不得报为正常停止）：并发启动合并及显式重启、协议版本不符、返回身份不符、始终未就绪、提前退出、拒绝退出、停止取消执行中／排队启动、崩溃后人工重启、不存在的程序。正常停止核实实际退出码 0，所有进程退出断言均在外围测试 Job 清理前完成。run_layout.py 另执行三轮九项窗口检查，核对仍有后台运行时退出应用的最终停止记录。

PostgreSQL 已新增独立实例适配（见下文）；Codex 和 QQ 尚未接入。多组件依赖顺序、真实端口冲突、发行版本清单和安装修复仍未实现；现有协议版本检查不等于完整包版本校验。生产数据库的事务排空、平台在途发送语义需由各适配器实现并单独验收。

## PostgreSQL 实际组件验证

PostgresComponent.cs 托管准备好的独立 PostgreSQL 实例：工作进程持有数据库启动 Job，使用官方 pg_ctl 启动，核验 postmaster.pid 指向的实际进程属于该 Job，再用 pg_isready 等待接受连接。pg_ctl 已退出不代表数据库退出，也不独立构成就绪证据。应用锁文件放在数据目录外；恢复过程不删除 PID 文件或关闭 fsync。

测试需已有完整 PostgreSQL 分发目录（bin/lib/share），不下载或注册 Windows 服务：

```powershell
python scripts/desktop_probe/run_postgres.py --exe .dev/refactor-r0/desktop/portable-postgres/Qiban.DesktopProbe.exe --bin <PostgreSQL的bin目录> --evidence .dev/refactor-r0/desktop/postgres-new
```

测试脚本在 Windows 临时目录创建权限受限的全新数据库，用目录联接引用现有完整 PostgreSQL 运行程序以验证 ASCII 路径；不更改源程序目录。随机密码仅在测试内存及初始化短时文件中存在，初始化后删除密码文件。证据保留无密码的 postgres-spec.json 和测试运行目录位置；它们是测试接口，不是生产配置迁移方案。

实际窗口可用 --postgres-spec 指向测试脚本生成的配置，与 --data-dir 分开。测试工具自动验证正常窗口及数据库失败时页面仍可用，无需人工点击。默认仍是合成服务模式。--postgres-host 与 --postgres-worker 为受控测试／托管入口，不是外部管理 API。

数据库正常停止使用 pg_ctl fast，回滚未提交事务并保留已提交资料；业务层停止接纳／排空尚未接入。停止确认除进程结束外还检查退出码，非零退出记录 stop_failed，不冒充 graceful_stop。异常退出由 Job 清理，随后用真实数据库恢复与读写验证资料；该测试不替代整机断电、大库或长时压力。

启动时完成真实接受连接检查；后续状态查询目前检查自有工作进程与数据库主进程存活，不代表 SQL、连接池或负载持续健康。多组件依赖、正式密钥库、安装资源分发和中文 Windows 用户路径仍待后续。历史阶段证据不再随源码保存。

## 加密存储与依赖组合

ComponentGroup.cs 接收应用代码提供的依赖顺序：先 PostgreSQL，后加密存储；正常停止逆序进行。前置组件失败不启动后续组件，后续组件失败会停止本次已启动的前置组件。停止取消当前启动和此前排队的组合启动；故障不会自动重启。当前是固定的两组件顺序，不提供外部任意依赖图或执行命令。

StorageComponent.cs 以受管进程托管 storage_worker.py。后台复用现有 PostgresStore／ContentCipher，通过明确安装 ID 从 Windows 凭据管理器取测试密码和密钥；先读取并解密 manifest，再验证关键资料，成功才报告 ready。命令仅支持状态查询和停止，不接入 Service 的角色、模型、平台或旧聊天身份。状态查询执行实际 SELECT 1 并读取 manifest；完整 SQL／资料压力仍需后续验证。

凭据使用独立测试命名空间 qiban-desktop-probe-storage，不访问旧日常命名空间 companion-local-service。该入口不迁移表、不初始化默认资料、不生成替代密钥。配置只含凭据标识、回环连接信息及路径；密钥和密码不通过命令行、网页或 C# 桥接传递。

```powershell
python scripts/desktop_probe/run_storage.py --exe .dev/refactor-r0/desktop/portable-bundle/Qiban.DesktopProbe.exe --worker <Qiban.Storage.exe路径> --bin <PostgreSQL的bin目录> --evidence .dev/refactor-r0/desktop/storage-new
python scripts/desktop_probe/run_components.py --group --exe .dev/refactor-r0/desktop/portable-storage/Qiban.DesktopProbe.exe --evidence .dev/refactor-r0/desktop/groups-new
```

测试自行创建全新数据库、合成加密资料和随机命名的 Windows 测试凭据，结束时删除该凭据。数据库／日志保留为无密钥证据。--stack-spec 接收测试工具生成的组合配置，桌面内显示两个组件状态；默认不启用。--stack-host、--storage-worker 和 --group-tests 为托管或验收入口。

2026-10-30 起 StorageSpec 只接收 WorkerExecutable、ConfigurationPath、LogDirectory，启动打包后的 Qiban.Storage.exe，不再接收开发 Python 或脚本路径。旧开发测试配置需由 run_storage.py 重新生成。按用户 D13 决定，不迁移旧测试数据库；正式安装凭据、权限最小化与完整管理服务仍待接入。


## 内置存储运行资源构建

先在项目专用 CPython 3.11 x64 venv 中安装固定依赖，再用该 venv 构建。以下均是开发命令，不是最终用户操作：

```powershell
python -m venv .dev/storage-build-python
.dev/storage-build-python/Scripts/python.exe -m pip install --require-hashes -r scripts/desktop_probe/storage-requirements-lock.txt
.dev/storage-build-python/Scripts/python.exe scripts/desktop_probe/build_storage.py --output .dev/storage-bundle-new --work .dev/storage-build-new
```

构建要求全新 output/work 目录，核对环境版本与锁文件完全一致；不会覆盖运行中的旧包。直接依赖意图在 storage-requirements-direct.txt，锁文件包含传递依赖和构建工具，当前只针对 CPython 3.11 Windows x64。构建产物是 Qiban.Storage 目录：内部 EXE、_internal 运行库与 JSON/SQL 资源、许可证、构建环境清单和逐文件 SHA-256。发布时必须携带整个目录，内部后台由主应用启动，不要求用户打开第二个窗口。SQL 资源随包保留，但当前后台仍只读打开已有测试库；正式首次建库另行接入。

StorageComponent 在启动前核对清单，文件缺失或摘要不符时报 storage_bundle_invalid，不回退到开发 Python。清单用于发现缺文件／混装，不作为发行签名或对抗恶意修改的信任根。--check-resources 是无需凭据的构建验收入口，检查内置配置、SQL 文件可读、加密往返及 PostgreSQL 二进制驱动。

run_storage.py 复制桌面及后台到中文目录，从无关工作目录启动，只给应用保留 System32 的 PATH，并用无效 PYTHONHOME 和外部影子模块验证其库搜索路径只来自包内。真实数据库验证使用复制后的程序；程序目录前后逐文件摘要保持不变。测试工具自身仍使用开发 Python 创建合成数据库，数据库程序仍使用外部测试分发目录，因此这些证据不等同于完整安装器或干净机器验收。


## 托管首次初始化与单入口整合验证包

```powershell
python scripts/desktop_probe/build_postgres.py --source <PostgreSQL18.6完整分发目录> --output .dev/postgres-resources-new
./scripts/desktop_probe/build.ps1 -ManagedApplication -OutputDirectory .dev/managed-desktop-new
python scripts/desktop_probe/assemble_application.py --desktop .dev/managed-desktop-new --storage <Qiban.Storage目录> --postgres .dev/postgres-resources-new --output .dev/managed-app-new
```

整合包双击 Qiban.exe 即准备空白资料，不要求额外模式参数或开发 Python。它仍是整合验证应用，不含完整角色／平台管理体验。`--data-dir` 仅用于开发测试的隔离资料；未传时使用现有 AppPaths 默认目录。完整资源目录必须随主程序保留。build.ps1 不加 -ManagedApplication 时仍构建原合成服务探针，以便既有隔离测试继续使用。

初始化以当前 Windows SID 和 SYSTEM 限制目录访问，用 FileLock 拒绝并发准备。凭据缺失或已完成数据库异常时明确失败，不重新生成密钥；尚未提交的 initdb 目录移入本安装 abandoned 留作诊断。初始化密码通过仅本机当前用户可访问的命名管道传递，配置和日志不记录密码。数据库日常账号无超级管理员或建库权限，初始化完成后关闭高权限数据库准备进程，再按组件依赖启动日常后台。

run_installation.py 验证 fresh／reopen、权限、端口与资源故障以及十个真实宿主终止节点，run_managed_launch.py 验证没有 --managed-install／--smoke 参数时的普通自动启动。测试脚本负责创建测试目录及最终清理自己的凭据，应用自身的重试不会删除或重设已有凭据。--test-pause 只用于故障注入，60 秒有界等待；安装状态不把这种测试暂停当作完成。

数据库程序的 UTF-8 声明应用于各进程，不更改系统语言设置。当前资源包完整保留所选 bin/lib/share，上游 EXE 未改写；版本、原始摘要与许可随包保存。声明要求 Windows 10 1903 或更新，最终最低系统版本仍需所有组件联合验收。

历史阶段证据不再随源码保存。正式安装器、资源下载／修复界面、WebView2 缺失时准备和跨机器安装仍待接入。
