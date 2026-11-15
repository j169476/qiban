# 微信版本维护

微信客户端升级优先维护 `src/companion_wechat/native/versions.json`。
此清单同时驱动 Python 账号检测、数据库读取版本范围和 C++ 发送组件。
`core_version.h` 是生成文件，不要直接编辑；其他文件不再维护独立的版本列表。

每个版本必须完整提供文件大小、SHA-256、PE 时间戳与映像大小、账号对象定位和
11 个具名地址。清单加载时拒绝重复身份、缺失地址、越界地址与未知 ABI；
文件指纹不符或尚未适配的版本不会尝试旧地址。这里的指纹仅识别已审查文件，
不替代发行商签名。

## 更新步骤

1. 从腾讯官方安装包取得目标 `Weixin.dll`，记录版本和完整指纹；保留前一已验证版本供离线比对。
2. 核对账号对象链、字符串布局、任务调度、文本构造、分配与释放的完整函数及其外部调用。
   地址变化且 ABI 保持一致时新增清单项；如果对象布局或调用约定变化，先审查实现，
   不要仅把新地址标为 `msvc-text-v1`。此 ABI 包含账号对象偏移、MSVC 字符串/共享指针及调度回调约定。
3. 可在独立审查环境安装 `capstone==5.0.9` 和 `pefile==2024.8.26`，运行：

   ```powershell
   python scripts/check_wechat_compatibility.py --reference <旧版Weixin.dll> --candidate <新版Weixin.dll>
   ```

   工具要求两份 DLL 指纹均在清单内；比对完整函数、保留函数内跳转，并列出外部引用。
   地址重定位被忽略不代表对应外部函数语义相同，仍需人工核对；结构比对也不能代替实际发送验收。
4. 生成配置并检查：

   ```powershell
   python scripts/generate_wechat_versions.py
   python scripts/generate_wechat_versions.py --check
   python -m pytest -q --confcutdir=tests_wechat tests_wechat
   scripts/build_wechat_native.cmd
   ```

   构建脚本可通过 `QIBAN_DESKTOP_PYTHON` 指定 Python。CI 和原生编译都会检查生成文件是否过期。
   清单及加载器进入组件来源签名；更新后必须重建发送桥、微信读取组件和存储后台，再组装应用。
5. 对新版本进行只读账号检测、受限数据库读取测试；发送验收须取得用户明确授权并限定接收会话。
   记录实际完成的验证范围，不能把“编译通过”写成“真实发送成功”。

## 0.9.5 验证范围

保留 4.1.15.13、4.1.15.56，新增本机实际安装的 4.2.0.13。
新版本已核对完整 DLL 指纹、账号对象链和相关函数结构；只读账号检测通过。
此轮未执行真实聊天发送或平台账号密码登录，真实平台行为仍需使用时验证。

微信登录继续由官方电脑客户端完成手机确认或扫码。栖伴不提供微信当前接口不支持的
独立短信或密码登录，也不保存微信登录密码。
