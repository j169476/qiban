# Windows 原生 QQ 探针支持代码

此目录保留 R0 验证入口与测试。同步、检查点、分页、触发和单次发送的 11 个 CJS 实现已迁入 src/companion_qq/native，本目录同名文件仅兼容导出；正文历史记录保留当时状态。正式分页入库已接线，原生后台通信与发送替换尚未完成。不含官方 QQ 二进制、上游桥接代码、账号或聊天样本。

## 群引用与固定范围分页

2026-10-30 用户选择 B：确实无法补齐历史时继续处理可读内容，并在模型上下文说明记录不完整及原因。group_window.cjs 现在对可识别的读取失败、空页、停滞、页数限制和缺失检查点返回已有 messages 与 history_status；reachedCheckpoint 不伪装成功。身份／来源冲突、异常协议、取消继续抛错。resolveCheckpoint 的 allowMissing 仅用于下界确实返回无匹配记录，上界和歧义仍严格核对。group_recovery.cjs 组合上述流程，不自行提交业务检查点或发送回执。

原因代码与 companion_core.history_status 共用契约：未知具体原因时明确未确认，不根据原生错误码推断删除或撤回。27 轮先读取真实两条样本，再注入后页失败，保留消息并报告 history_query_failed；28 轮组合入口注入下界无记录及后页失败，同时保留 checkpoint_unavailable 和 history_query_failed。均无发送／模型调用，故障为注入而非声称平台自然丢失消息。相关 Node 检查 49 项及组合入口 4 项通过。

2026-10-30 启动恢复修正：第 16 轮首次启动未匹配离线样本，16b 重启只读才查齐，不能将后者覆盖前者算稳定通过。17／18 空缓存复现全局同步结束后首次历史仍返回 -2，后续查询才有内容；新缓存内同一群序号的 msgId 改变。group_window.cjs 接收的 msgId 只限当前缓存查询定位，不能直接作为跨缓存持久身份或最终核心事件去重键。

group_checkpoint.cjs 保存账号、群、群序号、原作者两种身份及时间，恢复时用 getSingleMsg 按序号重新定位，再核对身份；缺失、真实消息歧义或不一致明确失败。group_head.cjs 等待全局同步之后目标群的会话更新，取得候选位置，再由 advanceGroupHead 合并 getLatestDbMsgs 的目标群快照后冻结上界。21 轮证明全局同步之后的会话通知仍可能滞后于已经收到的新消息，不能直接把第一条通知当作完整同步位置。上界也须按序号读取并核验，不能仅凭全局同步信号或等待固定秒数宣称群记录就绪。历史绑定检查、取消和同步代次检查保持生效。索引和门闩必须绑定当前账号／运行实例；这些辅助代码仍未接入产品层。

19／20 轮冷缓存单次通过后，21 轮真实旧缓存复测失败；不能用此前成功覆盖该失败。22—24 轮只读诊断确定：QQ 系统提示可与正常消息共用群序号，并使 getSingleMsg 返回多条。group_records.cjs 根据实测结构识别 msgType=5、空作者、纯 elementType=8 的系统记录；检查点解析排除这些记录，分页仍穿过它们继续寻找真实检查点。相同群序号不等于相同分页游标，分页用当前缓存 msgId 检测前进和循环；两条真实消息冲突仍拒绝。

新增六项回归后 Node 检查共 46 项通过。25 轮在失败缓存副本上把旧候选 5658 提升至实际已收到的 5660，跨三页穿过同序号系统提示，输出恰好 5659／5660；26 轮在未缓存这两条消息的旧快照副本上首次启动补收，两页得到相同两条，普通／@ 判断正确。两轮均正常退出，无延时重试、发送或模型调用。原始失败缓存与记录保持不变，副本验证不等于任意长服务端历史完整。首次安装空会话、不可取回的历史、撤回／缺失检查点等仍需正式接入处理。

group_identity.cjs 根据固定 Windows 内核实机样本核对直接 @ 的账号号码与内部身份；引用触发还需要同群、原作者两个身份及已确认发送的最终群序号。仅把自身发送状态回调交给 observeSent；暂态 sendStatus=1 的序号、引用预览内的 @ 和引用原文查询生成的记录编号都不是确认发送证据。当前索引仅在内存中，正式接入还需持久发送账本和账号运行代次绑定，不能直接替代核心发送授权。

group_window.cjs 在调用方指定下界与冻结上界后，使用 getMsgsIncludeSelf 的包含锚点向旧分页读取；按群序号排序、去掉跨页重叠，上界之后的消息留给下一批。只有实际找到已验证的下界序号与当前缓存定位编号才能报告 reachedCheckpoint；没有找到时按上述 B 规则保留不完整说明，不能把部分结果当作完整补收。调用方通过 assertCurrent 同时检查同步代次与取消。结果不证明任意服务端历史完整；永久记忆、发送防重与权限仍由核心负责。

运行 node --test scripts/qq_native_probe/group_identity.test.cjs scripts/qq_native_probe/group_window.test.cjs。15 项合成用例通过；连同同步与发送防重发共 29 项通过。实测证据 group-14f、group-15a、group-15b 和 group-15-identity-replay.json 保存在忽略目录：引用本人正例、引用他人含旧 @ 反例、三页重叠去重、翻页中新增消息进入下一范围及重启重读已通过单次验证。没有把该辅助代码接入正式 QQ 传输，微信跨页范围协议也尚未改动。

## 私聊恢复验证边界

private-29／32 在原测试缓存中读回 18 条私聊记录，包含锚点向旧分页九页，到达已知下界；记录中含系统消息，不把它算作普通用户发言。private-30／31 独立新缓存的最近读取持续 -2，本地最近和按旧序号查询成功但为空；具体原因未确认，不能视作空会话或历史已删除。私聊 msgSeq 的跨缓存稳定性尚未证明，不能复用群聊检查点规则。四轮均只读、正常退出，无发送或模型调用。下一项 private-33 待人工在测试桥接停机时发送两条新私聊标记，之后才启动原缓存恢复；准备完成不等于验收通过。

33c 收到两条不同编号但文字均为 A 的私聊，原 A/B 等待条件未满足，失败记录保留；33d 重启两页验证到旧下界，保留两条同文消息。34／35 实测 getMsgByClientSeqAndTime 定位两条新记录，新缓存本地 msgId 改变；调用参数参考[上游公开接口声明](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/services/NodeIKernelMsgService.ts)，只据此使用接口名称和参数事实，未复制实现。private_checkpoint.cjs 以账号／对端／作者身份、clientSeq、msgTime、msgRandom 核验定位结果；持久检查点不依赖 msgId 或 msgSeq。private_window.cjs 与群聊共用 message_window.cjs；下界无法定位时传 null，不把旧缓存序号应用到新缓存。36a 原缓存辅助代码验证通过；36b 全新缓存开始时本地已有新两条，旧下界不可取回，完整恢复检查失败，实际返回两条与不完整原因 checkpoint_unavailable／history_read_stalled。不能声称专用接口已验证远端拉取或旧记录已删除。相关 Node 62 项通过。原生收件转换已新增至 companion_qq.native_normalization／QQModule.accept_native；托管进程通信和发送仍待接入，默认传输尚未更换。

## 消息同步

在启动会话同步之前注册消息监听器，将每个消息事件名交给 MessageSyncBarrier.observe。收到 onNtMsgSyncStart 后进入同步中；onNtFirstViewMsgSyncEnd 只代表首屏，不作为完整就绪。仅同一同步周期的 onNtMsgSyncEnd 使 wait 成功。

查询历史前 await wait，保存返回的 generation；查询返回后 assertCurrent，拒绝跨重新同步的结果。连接失效或会话更换调用 invalidate。超时和用户取消是明确错误，不转换为空消息结果。此门闩只解决同步时序；持久游标、分页补收、重复事件和账号边界仍由后续适配器负责。

实机证据保存在忽略目录 .dev/refactor-r0/qq-native/sync-check-*；固定内核版本 9.9.28-46928。此次只查询唯一授权私聊的已知测试文件，没有发送消息。原生日志中的完成事件顺序用于验证，不把最新 20 条查询当成完整历史恢复证明。

运行针对性测试：node --test scripts/qq_native_probe/sync_barrier.test.cjs。

## 图片与生命周期

image_integrity.py 需要 Pillow，用于关联原生下载完成事件并区分 source_bytes 与 kernel_display_derivative。只有实际摘要/大小匹配时 source_bytes_verified 才为 true；非原图转换版本只证明本地可读，不能标为原件。缓存用实际 SHA-256，不把文件名中的源标识当成本地内容摘要。

supervise.py 复用仓库 ProcessScope，仅监督自有测试进程，区分正常退出、功能结束后退出超时及主动中断。真实账号与二进制只在忽略目录中运行；此目录不提供发送操作。

Python 测试：python -m unittest discover -s scripts/qq_native_probe -p "test_*.py"。上述模块是 R0 可复用验证支持代码，尚未接入产品运行层。

监督器进度协议已改为 <prefix>-status.jsonl：生产者以 UTF-8 追加一行完整 JSON，读者只消费以换行结束的记录并记住字节位置，不读取被反复原子替换的状态快照。此变更用于消除 Windows 上实际观察到的读者与文件替换冲突；终态退出与强制清理结果另存 supervision.json。已有证据前缀禁止覆盖。旧实验脚本如需复用，先适配协议并使用新前缀。

无人值守故障测试已覆盖半条状态记录、中文 UTF-8 分段写入、完整但损坏的记录、无进度超时及证据前缀保护；监督器共 11 项测试。同步模块共 10 项测试，包括单个消费者取消与 100 轮模拟重新同步。完整结果和实机边界见 ../../docs/QIBAN_R0_UNATTENDED_VALIDATION.md。

## 发送中断实验保护

send_attempt.cjs 在调用发送前，以独占文件写入并刷盘保存 unknown 记录。相同账号、目标、标记和正文摘要再次申请时返回 claimed=false；不同操作或损坏记录明确报错，不删除旧记录后重试。调用者必须仅在 claimed=true 时调用一次发送接口。重启后只读核对历史与接收端结果；unknown 不等于失败，不据此重发。

运行 node --test scripts/qq_native_probe/send_attempt.test.cjs。4 项测试覆盖首次持久记录、跨进程重启、身份／内容冲突和记录损坏。本模块用于 R0 发送故障实验，尚不是产品完整发送状态机。
