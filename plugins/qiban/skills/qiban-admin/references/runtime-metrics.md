# 核心阶段与用量观测

管理聊天按需调用 `runtime_metrics(days=1,limit=50)`；days 为最近滚动 1–30 天，limit 为最近 1–200 条明细。此工具不启动角色，不消费模型额度，不发通知。它不替代 `assistant_status` 的运行、心跳、动作额度及故障状态。

`groups` 按 kind（stage/model）和 stage 汇总调用尝试、成功/失败/取消/未结束次数、已知耗时及 token。`recent` 显示对应开始/结束时间、状态、稳定错误代码和已观测用量。最近明细数量不会截断汇总。明细保留 90 天，随后续观测清理；完整备份包括观测账本，恢复时未结束记录标为 interrupted，角色仍暂停。

- `known_tokens` 仅汇总 Codex `turn.completed.usage` 的合法数字；没有数据时为 null。`token_known_calls` 表示该字段有数值的调用数，小于 calls 说明总量不完整。
- cached_input_tokens 已包含于 input_tokens，reasoning_output_tokens 已包含于 output_tokens，不再次相加。
- 阶段耗时包含模型时间，stage/model 两种耗时不相加。普通调用、原生网页与配图调用均使用相同观测入口。
- running 表示没有记录结束，可能仍在运行，也可能进程中断；结合实际心跳判断，不根据这一字段恢复角色。
- 这不是订阅余额、实际收费或每日额度账本。金额始终未知；不把缺失数据解释成免费。额度仍由 policy.daily_model_cycles 与统一预算服务约束。
- 记录中不保留提示词、模型原文、Cookie 或完整原生事件。正常退出/失败时删除临时事件文件；强制终止宿主可能留下 worker 目录中的临时文件，排障时不要将其原样发到聊天或仓库。

CLI 不提供 usage 时报告未知，用稳定错误码与心跳排查，不重新调用模型来补统计。写账失败只报告本进程写失败计数，不能因此重发已经发送的动作。

事件格式参考 [Codex 非交互运行文档](https://learn.chatgpt.com/docs/non-interactive-mode)。这些数字反映后台 CLI 实际报告的用量，不包括当前人工聊天或故障通知聊天的宿主用量。
