# 智能表格（ksheet）工具完整参考文档

金山文档智能表格（ksheet）提供了一套完整的表格操作 API，支持对工作表、视图、字段、记录进行增删改查操作。

---

## 通用说明

### 智能表格特点

- 支持多种视图：表格视图、看板视图、日历视图、甘特图等
- 丰富的字段类型：文本、数字、单选、多选、日期、附件、关联等
- 适合结构化数据管理：项目管理、CRM、任务跟踪、库存管理

### 创建智能表格

通过 `create_file` 创建，`name` 须带 `.ksheet` 后缀，`file_type` 设为 `file`：

```json
{
  "name": "项目任务跟踪表.ksheet",
  "file_type": "file",
  "parent_id": "folder_abc123"
}
```

### 读取智能表格内容

通过 `read_file_content` 读取，返回 Markdown 表格格式：

```json
{
  "file_id": "file_ksheet_001"
}
```

返回的内容为结构化的 Markdown 表格，包含所有字段和记录数据。

### 更新智能表格

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
| 项目管理 | 任务分配、进度跟踪 |
| CRM 管理 | 客户信息、跟进记录 |
| 资产管理 | 库存台账、设备管理 |
| 审批台账 | 合同风险排查台账等 |

### 与 Excel 的选择建议

- 需要公式计算、数据透视 → 选 **Excel**
- 需要多视图、字段管理、看板展示 → 选 **ksheet**
- 需要做任务管理/项目跟踪 → 选 **ksheet**
- 需要做财务报表 → 选 **Excel**
