# 智能文档（otl）工具完整参考文档

金山文档智能文档（otl）提供了专属的内容写入接口，支持以 Markdown 格式向文档插入内容（标题、文本、列表等），系统自动转换为富文本格式。

---

## 通用说明

### 智能文档特点

- **推荐度**：⭐⭐⭐ **首选文档格式**
- 排版美观，支持丰富组件（标题、列表、待办、表格、分割线等）
- 适合图文混排、报告撰写、知识文档
- 是网页剪藏（`scrape_url`）的默认输出格式

### 创建智能文档

通过 `create_file` 创建，`name` 须带 `.otl` 后缀，`file_type` 设为 `file`：

```json
{
  "name": "项目周报.otl",
  "file_type": "file",
  "parent_id": "folder_abc123"
}
```

### 内容格式

智能文档的内容使用 **Markdown** 格式写入和读取。写入时系统自动将 Markdown 转换为智能文档的富文本格式。


### 读取智能文档

通过 `read_file_content` 读取，返回 Markdown 格式内容：

```json
{
  "file_id": "file_otl_001"
}
```

返回内容已自动转换为 Markdown，可直接用于 AI 分析。


## 智能文档专属接口

### 1. otl.insert_content

#### 功能说明

向智能文档写入内容（Markdown/文本）。支持从文档开头或末尾插入内容，写入时系统自动将 Markdown 转换为智能文档的富文本格式。


#### 调用示例

从开头写入：

```json
{
  "file_id": "string",
  "title": "项目周报",
  "content": "# 项目周报\n\n## 本周进展\n\n- 完成需求评审\n- 启动开发任务",
  "pos": "begin"
}
```

在末尾追加：

```json
{
  "file_id": "string",
  "title": "补充内容",
  "content": "## 补充说明\n\n以上数据截至本周五。",
  "pos": "end"
}
```

#### 参数说明

- `file_id` (string, 必填): 智能文档文件 ID
- `title` (string, 必填): 文档标题
- `content` (string, 必填): 写入内容，支持 Markdown 格式
- `pos` (string, 可选): 插入位置，默认 `begin`。可选值：`begin`（从文档开头插入）/ `end`（在文档末尾追加）

#### 返回值说明

```json
{
  "result": "ok"
}
```

> 判断写入是否成功：`result == "ok"` 或 `code == 0`。失败时返回 `error` 字段描述错误原因。


### 典型用途

| 场景 | 说明 |
|------|------|
| 报告撰写 | 周报、月报、总结报告 |
| 知识文档 | 技术文档、操作指南 |
| 网页剪藏 | `scrape_url` 自动生成的格式 |
| 会议纪要 | 支持待办事项跟踪 |
| 分析汇总 | 风险排查台账、客户反馈分析 |

---