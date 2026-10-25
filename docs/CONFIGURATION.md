# 角色、基础记忆与头像

测试版不附带作者的人物卡、基础记忆、关系设定或头像。项目名称和插件入口中的“栖伴”是项目标识，不会自动成为新用户的角色资料。

两个初始化入口使用相同的空白人物卡语义：

```yaml
schema_version: 1
id: main
name: ""
core:
  description: ""
  speaking_style: []
interests: []
foundation_memories: []
locked_fields:
  - name
  - core
```

首次启动角色前，在已认证管理网页的“共用人物卡与基础记忆”读取设定，再通过“编辑人物卡”在资料工作台填写名称、性格、表达方式和兴趣。基础记忆由用户自己创建并明确选入人物基线；不填写时为空。锁定字段由管理端修改，普通角色对话不能改写管理权限。

没有默认外观描述或参考图片。视觉身份引用需要本机准备素材并登记文件名与摘要，当前没有专用的一键头像配置向导；不能只在人物卡里填一个不存在的路径。首次测试可以先保持该字段缺省，完成文字角色配置后再接入自己的素材。

源码默认值位于 `src/bili_assistant/defaults/persona.yaml` 和 `src/companion_core/defaults.json`。修改源码默认值只影响新建资料，不会覆盖已存在的运行库。已有用户应从管理界面修改自己的资料。

人物基线可以由管理者明确发布到共享核心；普通会话记忆继续按来源、对象和读取权限保存。共用数据库不意味着跨平台聊天自动共享所有私人资料，也不意味着平台账号关联后获得回复授权。

## 本机文件

| 路径 | 用途 | 发布约定 |
| --- | --- | --- |
| `.companion-service.json` / `.companion-install.json` | 本机服务定位与安装记录 | 不提交 |
| `companion-service-data/` | 运行资料、加密备份等 | 不提交 |
| `bili-assistant-data/` | 初始资料与旧版迁移来源 | 不提交 |
| `plugins/qiban/runtime.json` | 本机 Python 和项目路径 | 不提交 |
| `.dev/qq/` / `.dev/wechat/` | 连接器配置、构建及诊断 | 不提交 |
| `.dev/release-private-archive/` | 本机历史文档与个人素材归档 | 不提交 |

微信数据库密钥只在读取子进程内存中使用，不写密钥文件、不生成整库明文缓存。QQ 的 OneBot Token 保存在系统凭证库。不要把凭据写入示例配置或提交到 Issue。
