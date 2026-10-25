# 微信模块

模块使用本机加密数据库只读收件，并通过受限原生桥接发送文本。密钥只留在独立读取子进程内存，不生成整库明文缓存；允许会话的新消息写入项目的加密 PostgreSQL。微信客户端及登录资料均由部署者自行准备。

这是有明确版本限制的实验模块。原生发送指纹目前对应微信 `4.1.15.13`、`4.1.15.56`；收件也有自己的版本与数据库结构检查，以实际探针结果为准。版本字符串相同不代表文件指纹一定通过。未知版本停止运行，不通过修改白名单盲目放行。

## 准备本机配置

安装独立读取运行时和编译桥接：

```powershell
python -m venv .dev/wechat-db-venv
.\.dev\wechat-db-venv\Scripts\python.exe -m pip install -r requirements/wechat-db.txt
.\scripts\build_wechat_native.cmd
.\scripts\test_wechat_native.cmd
```

构建需要 MSVC x64；最后一条命令运行原生状态机的模拟测试，不连接微信。源码与许可见 `src/companion_wechat/native/`。

当前连接器登记范围仍限定为一个私聊和一个群聊，内部别名为 `owner_main`、`family`。这些名称是沿用的实现别名，不表示发布包附带任何真实账号。

在 `.dev/wechat/database-locations.json` 填写本机微信账号资料根目录，格式示意：

```json
{"account_dirs":["C:/Users/YourName/Documents/xwechat_files/your_account"]}
```

在 `.dev/wechat/test-targets-intent.json` 填写初次定位线索：

```json
{
  "automatic_sending_enabled": false,
  "private": {"wechat_id_hint": "填写目标微信号"},
  "group": {"window_title_hint": "填写目标群名"}
}
```

路径和线索必须来自自己的账号，以上示例不能直接使用。读取端将线索解析为内部稳定 username；有歧义则拒绝绑定。意向文件中的 `automatic_sending_enabled:false` 是只读登记约束，不授予回复权限。

构建及文件准备后执行：

```powershell
.\.venv\Scripts\python.exe -m companion_wechat module configure
.\.venv\Scripts\python.exe -m companion_wechat module check
```

`offline_ready` 只证明配置文件和构建一致，不表示登录、收件或发送已经通过实测。账号登记仍需要实际运行的受支持微信，不能从截图昵称推断内部身份。只读探针和采集命令的参数可用 `db-probe --help`、`collect --help` 查看。

## 授权与运行

在管理网页或后台管理聊天核对已验证的个人/群内部 ID 并分别授权，再在角色控制聊天显式 `wechat_start`。`wechat_status` 查询状态，`wechat_stop` 停止。私聊额度与群有限参与采用 [统一规则](CONVERSATION_REPLY_AUTHORIZATION.md)。

发送要求机器人微信主窗口保持最小化，不抢焦点、不切聊天、不使用键鼠或剪贴板；点关闭按钮收进托盘不等同于最小化。当前仅支持单行文本，最多 300 字符且不超过 1023 个 UTF-8 字节；超限拒绝，不自动拆条。图片、语音、文件和任意联系人操作不在当前模块范围内。

原生任务回调和严格匹配的新数据库发送记录都成立才记为成功；它证明本机发送记录，不证明对方已读。结果不明、版本或账号变化时停止，不自动重发。DLL 为避免异步回调卸载风险驻留至微信正常退出，必要时按状态提示正常重启机器人微信。

收件游标持久保存，普通断线有限退避重连。重连后仅恢复仍满足授权版本和新鲜度约束的待处理消息，不补发全部历史。密钥重取仍仅在内存，账号无法确认时不会切换到其他登录账号。

实际私聊、群 @/引用、客户端重启、多账号和长时运行由部署者验收。新用户登记流程仍较依赖手工配置，这是首个测试版的已知限制。
