# PDF 文档（pdf）工具完整参考文档

本文件包含金山文档 MCP 中 PDF 文档的操作说明。

---

## 通用说明

### PDF 文档特点

- 仅支持通过 `upload_file` 来创建或（传 `file_id`）更新内容

### 读取 PDF 内容

通过 `read_file_content` 读取，系统自动提取文本内容并转为 Markdown：

```json
{
  "file_id": "file_pdf_001"
}
```

> PDF 读取的内容为提取的纯文本，复杂排版（表格、图片）可能存在精度损失。

### 写入 PDF 内容

通过 `upload_file` 传入 `file_id` 更新文件内容（三步上传流程）：

```json
{
  "drive_id": "string",
  "parent_id": "string",
  "file_id": "string",
  "size": 1024,
  "hashes": [
    { "sum": "string", "type": "sha256" }
  ]
}
```

### 适用场景

| 场景 | 说明 |
|------|------|
| 合同签署 | 最终版合同文件 |
| 财务报表 | 固定格式的报表文件 |
| 资料归档 | 归档用的只读文档 |

### 注意事项

- 写入 PDF 为全量覆盖，不支持局部编辑
- 如需频繁编辑，建议使用 otl 或 docx 格式，最终导出为 PDF
