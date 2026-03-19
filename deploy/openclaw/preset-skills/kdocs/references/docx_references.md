# 文字文档（docx）工具完整参考文档

本文件包含金山文档 Skill 中文字文档（docx / Word 格式）的操作说明。

---

## 通用说明

### 文字文档特点

- 兼容 Microsoft Word 格式
- 适合需要导出为 .docx 的场景
- 排版能力相对智能文档(otl)较基础

### 创建文字文档

通过 `create_file` 创建，`name` 须带 `.docx` 后缀，`file_type` 设为 `file`：

```json
{
  "name": "供应商合作协议.docx",
  "file_type": "file",
  "parent_id": "folder_abc123"
}
```

### 内容格式

文字文档的内容使用 **Markdown** 格式写入，系统自动转换为 Word 格式。读取时通过 `read_file_content` 自动转回 Markdown。

### 读取文字文档

通过 `read_file_content` 读取：

```json
{
  "file_id": "file_docx_001"
}
```

返回 Markdown 格式的文档全文，可直接用于 AI 分析（如合同条款审查、内容摘要等）。

### 写入/更新文字文档

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
| 合同协议 | 供应商合作协议、合同审查 |
| 正式报告 | 需要导出 Word 的正式文档 |
| 模板文档 | 基于模板填充内容 |

### 与智能文档（otl）的选择建议

- 需要美观排版、丰富组件 → 选 **otl**
- 需要兼容 Word 导出 → 选 **docx**
- 不确定 → 选 **otl**（默认推荐）
