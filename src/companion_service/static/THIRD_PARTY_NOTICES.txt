# 第三方来源与许可证

本项目自有代码及随仓库分发的白龙娘界面插画、SVG 图标采用 [MIT License](LICENSE)。第三方代码、第三方代码的派生实现及外部依赖继续适用各自的原始许可证；根目录 MIT 许可不替代或重新授权这些部分。保留所有随附的原始许可与声明。

尤其是 `src/companion_wechat/db_windows.py` 中改编的读取算法适用下述 Apache-2.0 条款，`src/companion_wechat/native/` 和 `scripts/wechat_native_probe/` 中改编自 helper 的部分继续适用其非商业许可。包含这些组件的完整发行包不能仅按 MIT 许可解释。

| 内容 | 来源与固定版本 | 许可与位置 |
| --- | --- | --- |
| 微信数据库读取中的布局与提取算法 | `fanyuantaier/wechatauto-replica`，提交 `d322c4a9b1124228820bab363e5676016f188fc6` | Apache-2.0；`src/companion_wechat/third_party/NOTICE.txt` 及同目录许可证 |
| 微信原生发送 ABI、指纹及账号链等改编代码 | `Prometheus-first/helper`，提交 `a81c495770b397646ba0661118927c83589c41fc` | 自定义非商业许可；`src/companion_wechat/native/LICENSE.helper` 及原始第三方声明；实验探针目录也保留同样许可 |

Python 依赖由安装器从各自发行源安装，不在源码包中捆绑。版本约束见 `pyproject.toml`、`constraints-windows.txt` 和 `requirements/`。SQLCipher、sqlcipher3、PostgreSQL、QQ/微信客户端以及 OneBot 桥接分别适用其发行方条款；本仓库不包含这些产品的登录资料或二进制安装包。

原生桥接没有包含或编译 MinHook。上游声明中列出的组件不代表全部进入本项目的受限实现；具体改动与范围见各目录 README 和 NOTICE。

## 界面素材

v0.9.3 管理界面使用设备已有的系统字体与浏览器通用无衬线字体，不下载或捆绑字体文件。本轮仅调整字号、字重、颜色及间距，未新增第三方字体或图片素材；系统字体仍适用设备与字体发行方的许可。

`src/companion_service/static/white-dragon-tea.png` 和 `icons.svg` 由本项目按 MIT License 分发，使用、修改及再分发时保留版权和许可声明。该授权不包括用户上传的角色图片、聊天内容或未随仓库分发的参考资料，也不改变第三方素材已有的权利。

## 设计参考

角色成长与学习流程曾参考以下项目的组织方式；本项目的相关流程为独立实现，未移入其整套运行环境或提示词。保留固定版本来源以便追溯：

- [chenluQwQ/bilibili-ai-bot](https://github.com/chenluQwQ/bilibili-ai-bot/tree/6d24b1c6f8c4b300ad62056ced4f4ae5f7b54640)：角色设定、用户关系与日常反思。
- [xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot/tree/0ea9a817e38ea6d5fa0229552da0f86497be42bb)：视频学习、知识归档和学习目标组织。
