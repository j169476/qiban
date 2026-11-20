# 第三方来源与许可证

## 桌面许可索引

| 主要上游项目 | 许可 |
| --- | --- |
| [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)、[Waylay](https://github.com/Micuks/waylay-qq-bridge) | Apache-2.0 |
| [helper](https://github.com/Prometheus-first/helper) | 自定义非商业许可 |
| [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) | GPL-3.0-or-later |
| [BGE-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[Xenova 量化模型](https://huggingface.co/Xenova/bge-small-zh-v1.5)、[ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT |
| [Whisper](https://github.com/openai/whisper)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)、[Kokoro 中文模型](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh)、[Tokenizers](https://github.com/huggingface/tokenizers) | Apache-2.0 |
| [silk-wasm](https://github.com/idranme/silk-wasm) | MIT，底层编解码器另附 Russell BSD 与 Skype SILK SDK 声明 |
| [PostgreSQL](https://github.com/postgres/postgres) | PostgreSQL License |
| [CPython](https://github.com/python/cpython) | Python Software Foundation License 及随附声明 |
| [.NET Runtime](https://github.com/dotnet/runtime)、[C#/WinRT](https://github.com/microsoft/CsWinRT)、[Node.js](https://github.com/nodejs/node) | MIT 及随附第三方声明 |
| [WebView2 SDK](https://www.nuget.org/packages/Microsoft.Web.WebView2)、[Windows SDK .NET](https://www.nuget.org/packages/Microsoft.Windows.SDK.NET.Ref) | 各自的 Microsoft 软件许可条款 |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause |
| [python-docx](https://github.com/python-openxml/python-docx)、[python-pptx](https://github.com/scanny/python-pptx)、[openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | MIT |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0-or-later，附打包例外 |

## 分发声明

本项目自有代码及随仓库分发的白龙娘界面插画、SVG 图标采用 [MIT License](LICENSE)。第三方代码、第三方代码的派生实现及外部依赖继续适用各自的原始许可证；根目录 MIT 许可不替代或重新授权这些部分。保留所有随附的原始许可与声明。

尤其是 `src/companion_wechat/db_windows.py` 中改编的读取算法适用下述 Apache-2.0 条款，`src/companion_wechat/native/` 和 `scripts/wechat_native_probe/` 中改编自 helper 的部分继续适用其非商业许可。包含这些组件的完整发行包不能仅按 MIT 许可解释。

| 内容 | 来源与固定版本 | 许可与位置 |
| --- | --- | --- |
| 微信数据库读取中的布局与提取算法 | `fanyuantaier/wechatauto-replica`，提交 `d322c4a9b1124228820bab363e5676016f188fc6` | Apache-2.0；`src/companion_wechat/third_party/NOTICE.txt` 及同目录许可证 |
| 微信原生发送 ABI、指纹及账号链等改编代码 | `Prometheus-first/helper`，提交 `a81c495770b397646ba0661118927c83589c41fc` | 自定义非商业许可；`src/companion_wechat/native/LICENSE.helper` 及原始第三方声明；实验探针目录也保留同样许可 |

Python 依赖由安装器从各自发行源安装，不在源码包中捆绑。版本约束见 `pyproject.toml`、`constraints-windows.txt` 和 `requirements/`。SQLCipher、sqlcipher3、PostgreSQL、QQ/微信客户端分别适用其发行方条款；本仓库不包含这些产品的登录资料或二进制安装包。

原生桥接没有包含或编译 MinHook。上游声明中列出的组件不代表全部进入本项目的受限实现；具体改动与范围见各目录 README 和 NOTICE。

## 旧聊天召回模型

栖伴随包提供 BAAI 的中文 BGE-small-zh-v1.5 模型，使用 Xenova 修订 `75c43b069aac4d136ba6bc1122f995fedcfd2781` 的量化 ONNX 权重和分词器。上游标明模型使用 MIT 许可，原许可随模型保存在 `src/companion_core/history_model/LICENSE.txt`。桌面后台同时保留 ONNX Runtime、NumPy、Tokenizers 及其依赖的许可文本。[模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5) · [ONNX 来源](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)

## 界面素材

v0.9.3 管理界面使用设备已有的系统字体与浏览器通用无衬线字体，不下载或捆绑字体文件。本轮仅调整字号、字重、颜色及间距，未新增第三方字体或图片素材；系统字体仍适用设备与字体发行方的许可。

`scripts/desktop_probe/Assets/white-dragon-tea.png` 由本项目按 MIT License 分发，使用、修改及再分发时保留版权和许可声明。该授权不包括用户上传的角色图片、聊天内容或未随仓库分发的参考资料，也不改变第三方素材已有的权利。

## 设计参考

角色成长与学习流程曾参考以下项目的组织方式；本项目的相关流程为独立实现，未移入其整套运行环境或提示词。保留固定版本来源以便追溯：

- [chenluQwQ/bilibili-ai-bot](https://github.com/chenluQwQ/bilibili-ai-bot/tree/6d24b1c6f8c4b300ad62056ced4f4ae5f7b54640)：角色设定、用户关系与日常反思。
- [xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot/tree/0ea9a817e38ea6d5fa0229552da0f86497be42bb)：视频学习、知识归档和学习目标组织。

## R0.6 桌面容器实验依赖

`scripts/desktop_probe/` 的自有验证代码采用 MIT。固定引用 Microsoft.Web.WebView2 SDK 1.0.4314.35，其原始许可见 `scripts/desktop_probe/licenses/WebView2-SDK.txt`，不改为 MIT。构建出的本地实验包自包含 .NET 10.0.14；`build.ps1` 将对应运行时包的 LICENSE 和 THIRD-PARTY-NOTICES 原样复制到输出目录。WebView2 Runtime 使用设备已有安装，其分发与使用仍适用微软条款。本实验不构成最终安装包的分发审查。

## R0.5 本地语音实验依赖

`scripts/speech_probe/` 为自有 MIT 验证脚本；权重和第三方运行库仅保存于忽略的本地验证目录，不随源码导出。sherpa-onnx 2.0.1 和 Kokoro v1.1-zh 权重适用 Apache-2.0，faster-whisper／Whisper base 适用其 MIT 许可。模型包含未调用的 espeak 数据，不能将整包统一重新授权。依赖版本、原始来源和正式资源分发前的清单要求见 `scripts/speech_probe/README.md`。

语音 GPU 实验在忽略目录内复用本机 PyTorch 安装所带的 NVIDIA cuBLAS 12 动态库，保留来源包 LICENSE／NOTICE 和逐文件摘要；这些库不随源码导出，也不由自有 MIT 许可重新授权。正式运行资源包仍须明确 NVIDIA 组件的分发条款和清单。

## R0.5 文件解析实验依赖

`scripts/file_probe/` 是自有 MIT 验证代码。解析依赖为 pypdf、python-docx、openpyxl、python-pptx、defusedxml；ReportLab 仅生成合成测试文件。直接验证版本见该目录 requirements.txt。第三方库使用各自原许可，仅安装于开发／忽略目录，不随此次源码导出。最终安装包须保留其许可和传递依赖声明。


## R1 内置存储后台实验包

`scripts/desktop_probe/build_storage.py` 使用 PyInstaller 6.22.3 构建 Windows x64 的目录式后台程序。PyInstaller 的 GPL 例外允许生成的应用按自身及所包含依赖的许可证发布，不把本项目自有代码改为 GPL；原文见 [PyInstaller 许可说明](https://pyinstaller.org/en/stable/license.html)。构建工具和依赖版本及 wheel 摘要固定在同目录 `storage-requirements-lock.txt`。

与旧版 Python 源码安装方式不同，此本地实验二进制包包含 CPython 运行时及必要 Python／原生库。构建脚本随包复制 CPython LICENSE、各依赖分发包提供的 LICENSE／COPYING／NOTICE 和项目声明；`build-packages.json` 列出构建环境版本及原文位置，是保守超集，不声称所有构建工具都运行在后台中。Psycopg、OpenSSL、密码库及其他原生依赖继续适用其上游许可，不能统一解释为 MIT。此实验包未公开分发；最终安装包仍须结合全部组件完成许可与来源审计。


## R1 内置 PostgreSQL 资源实验包

`scripts/desktop_probe/build_postgres.py` 从已取得的 PostgreSQL 18.6 Windows x64 分发目录复制 bin、lib、share，省略 pgAdmin、StackBuilder 和独立管理工具界面。随包保留 `server_license.txt`、`commandlinetools_3rd_party_licenses.txt` 和原始文件 SHA-256。PostgreSQL 及随附原生库继续适用分发包中的原始许可，不改为 MIT。

为支持非 ASCII 安装／资料目录，在副本的 EXE 旁加入自有 UTF-8 Windows 应用声明，不修改上游 EXE 内容、系统区域设置或注册表；说明依据为微软的[应用进程 UTF-8 配置](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page)和[外置应用声明](https://learn.microsoft.com/en-us/windows/win32/sbscs/application-manifests)。该机制要求支持 activeCodePage 的 Windows 10 1903 或更新系统；最终系统支持范围仍须结合 .NET、WebView2 和平台组件联合验证。

## R2 Windows 身份验证接口

桌面验证包使用 Microsoft.Windows.SDK.NET.Ref 10.0.22621.57 提供的 Windows API 投影和 WinRT.Runtime；Windows 自带的身份验证组件不由项目重新实现。SDK 包的原始许可地址为 https://aka.ms/WinSDKLicenseURL，下载到 `scripts/desktop_probe/licenses/Windows-SDK.rtf` 并以 SHA-256 固定；C#/WinRT 的 MIT 声明保存在同目录 `CsWinRT-LICENSE.txt`，来源为 Microsoft/CsWinRT 仓库。桌面构建同时复制这两份原文。SDK 组件沿用其上游条款，不由 Qiban 的 MIT 声明重新授权。

## R3 独立 ChatGPT 授权验证

身份令牌验证使用 PyJWT 2.15.1（上游 MIT 许可），RSA 密码运算使用已固定的 cryptography。PyJWT wheel 的 SHA-256 已加入存储后台依赖锁；构建脚本复制其原始 LICENSE。授权实现为 Qiban 自有代码，不打包任何账号凭证。


## QQ 原生运行层迁入（2026-10-31）

`src/companion_qq/native/listener.cjs` 来自 Waylay 的 `listener.js`，`session.cjs` 和 `login_session.cjs` 的初始化流程改编自其 `src/bridge.js`，固定提交 `3dd3572a4e34f5b2f91ad6d645a16fe6cc34eadb`；这些文件按 Apache-2.0 保留来源，许可原文随包位于 `src/companion_qq/native/LICENSE.Waylay`。本次修改包括固定 Windows 版本、明确账号及目标核验、独立扫码登录、会话和群历史调用。其余自有同步、分页、检查点、IPC、扫码状态机和收件代码为 MIT。

`qqnt-lifecycle.c` 与 `QQNT-lifecycle.def` 为此前自有实验生命周期转接层，MIT。源码包包含源文件，不包含测试账号、官方 QQ 内核或 Node 二进制；本机实机验证复用忽略目录中按摘要核验的运行资源。正式运行资源分发清单与安装集成仍须完成，不以源码包的 MIT／Apache 声明重新授权官方组件。



## 正式文件解析接入（2026-10-31）

文件解析代码已迁入 src/companion_core/file_reader.py，自有 MIT；原 scripts/file_probe/reader.py 保留兼容入口。独立后台依赖锁新增已验证的 pypdf、openpyxl、python-pptx、defusedxml、python-docx 及传递依赖 lxml、et_xmlfile、XlsxWriter，固定完整 wheel 哈希。随本地验证包保留各依赖原始 LICENSE／NOTICE，沿用各自上游条款，不改为 MIT。ReportLab 仍仅生成测试文件，不加入正式后台依赖。


## 内置本地语音工作进程（R5）

自有 speech_worker、speech_decode 与构建脚本使用项目 MIT。CPU 识别采用固定版本 faster-whisper／CTranslate2／Whisper base，模型修订与逐文件摘要见 scripts/speech_probe/asr-resources-lock.json，完整构建依赖及 wheel 摘要见 speech-requirements-lock.txt。冻结包保留各 Python／本地依赖的许可与第三方声明，模型卡与 Whisper 许可单独保留。

SILK 使用 silk-wasm 3.7.1（MIT），同时保留其 Russell BSD 声明及底层 Skype SILK SDK 的原始声明，不能把底层 SDK 一概重标 MIT。Node 原文声明随资源包保留。来源为 https://github.com/idranme/silk-wasm/tree/v3.7.1 及其 binding/libSilkCodec/silk/src/SILK_SDK_SRC_ARM_v1.0.9/interface/SKP_Silk_SDK_API.h；Whisper 模型卡固定到 https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66 。此本机验证包不构成对所有分发条件的额外保证。Kokoro 中文 CPU 合成已纳入独立语音构建锁，使用 sherpa-onnx/core 2.0.1，保留模型 Apache-2.0 声明并排除未使用的 espeak 数据和英文词典；CUDA 加速资源仍在后续整合范围。完整识别及合成资源见 scripts/speech_probe/speech-resources-lock.json。

## B 站桌面活动组件

独立开发包沿用 bilibili-api-python 17.4.2。原库按 GPL-3.0-or-later 分发。包内 resources/storage/licenses/bilibili-api-python 保留发行版原许可，resources/storage/upstream-source/bilibili_api_python-17.4.2.tar.gz 是经 SHA-256 核对的对应上游源码。来源见 [版本发布页](https://pypi.org/project/bilibili-api-python/17.4.2/)。

栖伴自有代码沿用 MIT，源文件中的上游声明继续适用。resources/storage/upstream-source/qiban-sources.zip 随本开发包提供 Qiban 源码与桌面构建文件。qrcode-terminal 0.8 的发行包在元数据中声明 MIT，原始归档没有附单独的 LICENSE 文本；包内保留原始元数据和完整上游源码。其他依赖的实际许可原文与版本清单随后台分发。公开发布前仍按最终交付物核验对应源码、构建文件及各组件的分发义务。
