# QQ 模块

`companion_qq` 是独立 OneBot v11 连接器。当前支持文本私聊与群聊，不包含 QQ 客户端或桥接程序。部署者自行准备兼容桥接，例如 NapCat；协议接入与 QQ 客户端稳定性需要在自己的环境验证。

## 接入

1. 为机器人使用独立 QQ 账号，准备支持 HTTP API 和正向 WebSocket 事件的桥接。
2. 两个接口只监听回环地址，启用同一个高强度 Token。不要把未认证接口暴露到局域网或公网。
3. 在项目根目录配置意向目标。以下号码为示例，必须替换为自己的真实账号：

```powershell
.\.venv\Scripts\python.exe -m companion_qq configure --account 10001 --friend 10002 --group 20001
.\.venv\Scripts\python.exe -m companion_qq set-token
.\.venv\Scripts\python.exe -m companion_qq probe
```

`set-token` 隐藏输入并保存到系统凭证库。默认接口是 `http://127.0.0.1:3000` 和 `ws://127.0.0.1:3001`，可通过 `--http`、`--ws` 调整。`probe` 只核验登录身份、好友及群元数据，不发消息、不启动角色、不授予权限。

4. 在管理网页或后台管理聊天读取候选并明确授权。按需配置私聊额度和群参与策略，见 [统一授权说明](CONVERSATION_REPLY_AUTHORIZATION.md)。
5. 在角色控制聊天显式调用 `qq_start`；`qq_status` 查看状态，`qq_stop` 停止。

本地配置写入 `.dev/qq/module.json`，不会随源码发布。仓库中的 `scripts/start_qq_bridge.ps1` 是固定版本开发辅助启动器，需要自行准备对应上游文件，并不是通用安装器。使用独立部署的 OneBot 服务无需运行它。

## 多账号与恢复

连接器核对配置中的机器人 QQ 号和桥接实际 `self_id`，并在发送边界复验。桌面同时登录另一个 QQ 不决定机器人身份，桥接实际登录的账号才是依据；身份不符会停止，不能自动换号。开发环境曾完成有限私聊验证，但这不等于所有双账号组合已验收。

WebSocket 断线可退避重连；收件 ID 和待处理状态持久保存。发送超时或结果不明不会自动重发。群触发解析只接受真实结构化 @ 或可验证的引用，普通字符串“@昵称”不算触发；回复以文本段提交，不把模型输出作为 CQ 指令解释。

建议人工依次验证：未授权静默、授权私聊、群 @ 唤起、参与阶段结束、撤销后停止、大小号同时登录、连接中断恢复。长时验收由使用者完成。
