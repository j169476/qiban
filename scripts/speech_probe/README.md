# R0.5 本地语音验证

自有脚本 MIT。仅用于合成文本的本地合成／识别和性能检查，不调用平台发送、付费 API 或角色模型。不是生产语音适配器。

## 环境

Windows x64、Python 3.11。本次使用项目原有 faster-whisper 环境，sherpa-onnx 单独安装在 .dev/refactor-r0/speech/python。可在独立虚拟环境中安装 requirements.txt 复现；requirements.txt 固定直接验证组件，尚非全传递依赖发布锁文件。

模型来源、固定版本与合成模型压缩包官方 SHA-256 见 model_sources.json。下载合成模型后先校验大小和 SHA-256，再用 tarfile.extractall(..., filter="data") 解压到忽略目录。识别模型使用 Systran/faster-whisper-base 的固定提交；本次复用已有完整文件并按 Hugging Face 缓存元数据验证哈希，复制到独立目录。运行脚本只从本地加载权重，缺少模型会报错，不自动联网补下载。

## 运行

从仓库根目录执行，evidence 目录每次使用新名称：

```powershell
python scripts/speech_probe/run_probe.py --evidence .dev/refactor-r0/speech/tts-new --dependencies .dev/refactor-r0/speech/python -- tts --model .dev/refactor-r0/speech/models/kokoro-int8-multi-lang-v1_1
python scripts/speech_probe/run_probe.py --evidence .dev/refactor-r0/speech/asr-new --dependencies .dev/refactor-r0/speech/python -- asr --model .dev/refactor-r0/speech/models/faster-whisper-base --audio .dev/refactor-r0/speech/tts-new --device cpu
```

显卡实测将 device 改为 cuda；缺失运行库会明确失败，不悄悄改成 CPU。--timeout 2 可注入两秒超时；是否在加载后被终止，必须检查 loaded.json，不能仅根据退出码推断。

worker.py 生成四段固定中文样本（含原始数字日期），返回单声道 24 kHz PCM WAV；先检查非空、有限采样值和非静音，再写文件。识别记录原始文本和分段，不把明显错字修饰成正确答案。监督器使用现有 Windows Job 组件限制工作进程生命周期，正常和异常均精确清理自有进程。

## 已知问题

- Kokoro 的 FST 加载器在本机不能打开中文绝对路径。工作进程显式以模型目录为工作目录、使用固定相对文件名，已复验通过；无需用户更改安装目录。
- faster-whisper-base 的四段合成回读存在同音字和年份错误，不能作为默认中文识别质量已通过。合成回读不代替真人／噪声／长语音评测。
- 原 CUDA 失败来自 v13 系统目录与 v12 动态库要求不匹配；已在项目隔离目录补入本机现有 cuBLAS 12 库并完成真实 GPU 转写。
- 用户已确认样本 1／4 的基础音色与数字发音正常；这里只测试中文女声 sid=3，不代表已选定最终产品音色。
- 合成包带有未使用的 espeak-ng-data，本验证未配置／调用它，也未把模型包加入源码发布。正式资源包应仅选取验证所需资产并补齐逐项第三方声明。模型权重 Apache-2.0，识别权重 MIT；自有 MIT 不替代依赖许可。

历史验证记录已清理；语音识别准确率仍需独立验收。资源准备和脚本运行均属于开发验证，不是最终用户安装步骤；最终由应用内组件管理器完成。

## 指定进程内 CUDA 库目录

```powershell
python scripts/speech_probe/run_probe.py --evidence .dev/refactor-r0/speech/gpu-new --dependencies .dev/refactor-r0/speech/python -- asr --model .dev/refactor-r0/speech/models/faster-whisper-base --audio .dev/refactor-r0/speech/tts-cpu-03 --device cuda --rounds 3 --dll-dir .dev/refactor-r0/speech/gpu-runtime
```

--dll-dir 可重复，用于明确指定已有匹配版本的目录；仅修改工作进程的 DLL 搜索路径和 PATH。当前识别引擎需要 cuBLAS 12，不能把 CUDA 13 DLL 改名替代。result.json 记录 actual_device、actual_compute_type 和实际加载的 GPU 库路径；--rounds 1–5 分别记录每轮，区分第一条初始化与预热后的时间。GPU 成功不会自动消除识别质量问题。TTS 探针仅支持 CPU，显式指定 cuda 会报错。
