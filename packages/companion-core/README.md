# 独立角色核心

从仓库根目录构建：`python -m pip wheel --no-deps --no-build-isolation packages/companion-core -w dist`。

此分发仅包含 `companion_core`，不包含 `bili_assistant`、B站 SDK 或 `companion_codex`。应用注入模型端口、宿主端口与平台端口。默认持久化实现为 SQLite 和可编辑 YAML。

`Companion` 提供显式启动、事件处理、跨平台身份评估及动作派发。`Companion.cycle()` 使用 `orchestration.Workflow/Stage` 注册的平台工作流，核心负责阶段优先级、周期互斥、逐阶段守卫、共享模型预算和多平台轮转。没有专用工作流的平台采用通用收件与对话阶段；学习、媒体等扩展能力由平台注入服务。`Store` 可单独用于记忆、人设、关系、分片检索和备份。默认不启动模型、后台或任何平台连接。

既有 B站入口和独立核心路径执行同一组工作阶段。插件的宿主监护和独立收件进程继续使用兼容层，实际平台网络调用仍由 B站适配器处理；QQ/微信尚未接入。构造 `bili_assistant.core_binding.companion(engine)` 只创建绑定，仍需显式 `start()` 和 `cycle()`，不能与既有运行同时接管。日常插件用户继续使用唯一的 `assistant_start` / `assistant_pause` 入口。

现有插件使用同仓库的整合分发；独立核心 wheel 供单独应用使用，避免在同一环境混装两个分发对同一包的不同版本。
