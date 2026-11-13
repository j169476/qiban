# R0.5 文件解析探针

自有 MIT 代码。验证 TXT/MD/CSV、文本 PDF、DOCX/XLSX/PPTX 的基础文字提取与出处。不接入用户资料、角色模型或平台发送，也不是完整文档理解产品。

## 运行

Python 3.11 / Windows x64。将 requirements.txt 安装到独立环境或 .dev/refactor-r0/files/python；reportlab 只用于生成合成测试文件。固定的是本轮直接验证依赖，不是最终发行包的完整传递依赖锁。

```powershell
python scripts/file_probe/run_probe.py .dev/refactor-r0/files/smoke-01/inputs/sample.pdf --evidence .dev/refactor-r0/files/pdf-new --dependencies .dev/refactor-r0/files/python
```

证据目录必须是新目录。result.json 返回提取状态、units、source、truncated 与范围说明；supervision.json 返回进程退出和超时事实。退出 0 不表示文件已完整读取，调用方还必须检查 result.status。读取失败不会调用备用服务或模型猜测内容。

run_probe.py 使用现有 Windows Job 管理工作进程，默认 20 秒、512 MiB 总提交内存；将 OpenBLAS／OMP／MKL 线程固定为 1，避免开发环境中可选 NumPy 导入引起线程内存预分配。只约束该子进程。

## 内容与限额

- 输入 10 MiB；Office ZIP 总解压 32 MiB、2000 个条目、单 XML 8 MiB。拒绝危险归档名称、异常压缩比、宏组件及 DTD／实体。
- 输出 10 万字符、2000 个片段；PDF／PPTX 100 页／幻灯片；Excel 最多扫描 10000 个单元格（含空格）。超限明确 truncated。
- 文本接受 UTF-8、UTF-8 BOM、UTF-16 BOM；其他编码明确 unsupported_encoding，不猜测乱码。
- PDF 提取文字并保留页码；没有文字层时返回 no_extractable_text，区分“可能需要 OCR 或为空”，不假称识别了图片。
- Word 提取正文段落和表格，保留块号／行列；不含页眉页脚、批注、脚注及图片。
- Excel 保留工作表和单元格；公式返回表达式并标记未求值，不执行公式、宏或外部链接。
- PowerPoint 保留幻灯片、形状和表格行列；不含备注、图像文字。
- 旧 DOC/XLS/PPT 明确不支持。加密 PDF 返回 encrypted；旧复合容器包装的 Office 文件返回 encrypted_or_legacy_container，不尝试解密。

## 检查

使用安装了 requirements.txt 和 pytest 的开发环境运行：

```powershell
python -m pytest scripts/file_probe/test_reader.py -q
```

若依赖采用 --target 安装，须在运行 pytest 前将其加入 PYTHONPATH。受控进程测试目前按仓库 .dev/refactor-r0/files/python 查找测试依赖。合成夹具覆盖七种格式、中文 PDF、文档／幻灯片表格、单元格公式、CSV 多行字段、扫描件、加密／旧格式／损坏文件、资源限额、XML 实体、宏及路径异常，并实际启动七种解析工作进程。

所有文档文字标记为 untrusted_document_data，仅作为数据返回；这个字段不是生产授权边界。跨会话文件访问授权与模型输入治理在后续产品接入阶段单独验证。

完整结果见 ../../docs/QIBAN_R0_FILE_PROBE.md。
