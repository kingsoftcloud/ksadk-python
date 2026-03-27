# 智能文档（otl）工具完整参考文档

金山文档智能文档（otl）提供了专属的内容写入接口，支持以 Markdown 格式向文档插入内容（标题、文本、列表等），系统自动转换为富文本格式。

---

## 通用说明

### 智能文档特点

- **推荐度**：⭐⭐⭐ **首选文档格式**
- 排版美观，支持标题、列表、待办、表格、分割线等丰富块组件
- 适合图文混排、报告撰写、知识文档、会议纪要等场景
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

创建完成后用下文 **`otl.insert_content`** 写入 Markdown/文本。**勿**对 `.otl` 使用 `upload_file`：该工具面向本地文字/表格/演示/PDF 文件上传，不支持 `.otl` 智能文档。

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

向智能文档写入 Markdown/纯文本内容。支持从文档开头或末尾插入，写入时系统自动转换为智能文档富文本格式。

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
- `title` (string, 可选): 文档标题
- `content` (string, 必填): 写入内容，支持 Markdown 或纯文本
- `pos` (string, 可选): 插入位置，默认 `begin`。可选值：`begin` / `end`

#### 返回值说明

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "result": "ok"
  }
}
```

> 常见成功判断方式：`code == 0`，且 `data.result == "ok"`。

### 2. otl.block_insert

#### 功能说明

向智能文档插入一个或多个块，适合在指定父块下按位置追加段落、列表、表格等结构化内容。

#### 调用示例

```json
{
  "file_id": "string",
  "params": [
    {
      "blockId": "doc",
      "index": 0,
      "content": [
        {
          "type": "paragraph",
          "content": [
            { "type": "text", "text": "hello" }
          ]
        }
      ]
    }
  ]
}
```

#### 参数说明

- `file_id` (string, 必填): 智能文档文件 ID
- `params` (array, 必填): 插入操作列表，每项为一个插入块对象
- `params[].blockId` (string, 常用): 目标父块 ID，例如 `doc`
- `params[].index` (integer, 常用): 插入位置索引
- `params[].content` (array, 常用): 待插入的块内容数组

#### 返回值说明

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "...": "..."
  }
}
```

> 返回结果会根据插入内容和文档状态有所不同，调用方通常以 `code == 0` 作为成功判断。

### 3. otl.block_delete

#### 功能说明

删除一个或多个块区间，适合按父块和索引范围删除内容。

#### 调用示例

```json
{
  "file_id": "string",
  "params": [
    {
      "blockId": "父blockId",
      "startIndex": 0,
      "endIndex": 1
    }
  ]
}
```

#### 参数说明

- `file_id` (string, 必填): 智能文档文件 ID
- `params` (array, 必填): 删除操作列表，每项为一个删除区间对象
- `params[].blockId` (string, 常用): 目标父块 ID
- `params[].startIndex` (integer, 常用): 删除起始索引
- `params[].endIndex` (integer, 常用): 删除结束索引

#### 返回值说明

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "...": "..."
  }
}
```

### 4. otl.block_query

#### 功能说明

查询指定块的结构与内容，适合在更新前先读取目标块信息。

#### 调用示例

```json
{
  "file_id": "string",
  "params": {
    "blockIds": ["目标blockId"]
  }
}
```

#### 参数说明

- `file_id` (string, 必填): 智能文档文件 ID
- `params` (object, 必填): 查询参数对象
- `params.blockIds` (array, 常用): 要查询的块 ID 列表

#### 返回值说明

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "...": "..."
  }
}
```

### 5. otl.convert

#### 功能说明

将 HTML、Markdown 等内容转换为智能文档块结构，适合在正式插入前先生成可复用的块内容。

#### 调用示例

```json
{
  "file_id": "string",
  "params": {
    "...": "..."
  }
}
```

#### 参数说明

- `file_id` (string, 必填): 智能文档文件 ID
- `params` (object, 必填): 转换参数对象。根据待转换内容类型填写对应字段

#### 返回值说明

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "...": "..."
  }
}
```

### 典型用途

| 场景 | 推荐工具 |
|------|------|
| 快速写报告、周报、纪要 | `otl.insert_content` |
| 在现有文档头部/中间精确插块 | `otl.block_insert` |
| 删除指定块或块区间 | `otl.block_delete` |
| 读取指定块结构 | `otl.block_query` |
| 将 HTML / Markdown 先转为块结构再复用 | `otl.convert` |

---