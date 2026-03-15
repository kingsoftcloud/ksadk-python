---
name: kdocs
description: 金山文档，提供金山文档操作能力。当用户需要操作金山文档时使用此 skill，包括：(1) 创建各类在线文档（智能文档、Word、PDF、智能表格、Excel）和文件夹(2) 查询、搜索文档空间与文件 (3) 管理空间节点、文件夹结构 (4) 读取文档内容 (5) 编辑操作各类在线文档（智能文档、Word、PDF、智能表格、Excel）（6）移动操作各类在线文档（智能文档、Word、PDF、智能表格、Excel）
homepage: https://365.kdocs.cn/latest
metadata: {"openclaw":{"primaryEnv":"KDOCS_TOKEN","category":"kdocs", "tokenUrl":"https://365.kdocs.cn/latest","emoji":"📝"}}
---

# 金山文档 MCP 使用指南

金山文档 MCP 提供了一套完整的在线文档操作工具，支持创建、查询、读取、编辑、分享、移动多种类型的在线文档。

## 支持的文档类型

| 类型 | 文件后缀 | 推荐度 | 说明 |
|------|----------|--------|------|
| **智能文档** | .otl | ⭐⭐⭐ **首选** | 排版美观，支持丰富组件 |
| 表格 | .xlsx | ⭐⭐⭐ | 数据表格专用 |
| PDF文档 | .dpdf | ⭐⭐⭐ | PDF文档专用 |
| 文字文档 | .docx | ⭐⭐⭐ | 传统格式，排版一般 |
| 智能表格 | .ksheet | ⭐⭐⭐ | 高级结构化表格，支持多视图、字段管理 |

## 文档类型选择决策树

```
用户需要创建文档
├── 需要丰富排版/图文混排？ → otl（智能文档）⭐首选
├── 需要表格/数据处理？
│   ├── 简单表格数据 → excel
│   └── 需要多视图/字段管理/看板 → ksheet（智能表格）
├── 需要生成 PDF？ → pdf
├── 需要兼容 Word？ → docx
└── 不确定 → otl（智能文档）⭐默认推荐
```

## 工具总览

### 写文档（7 个工具）

| 工具名 | 功能 | 参考文档 |
|--------|------|----------|
| `create_file` | 创建文件（支持 otl/docx/excel/ksheet/pdf） | `references/api_references.md` |
| `upload_file` | 仅更新已有文件（传 file_id + content_base64，服务端三步上传后返回最终结果） | `references/api_references.md` |
| `scrape_url` | 网页剪藏，抓取网页内容保存为智能文档 | `references/api_references.md` |
| `scrape_progress` | 查询网页剪藏任务进度 | `references/api_references.md` |

### 读文档（2 个工具）

| 工具名 | 功能 | 参考文档 |
|--------|------|----------|
| `list_files` | 获取文件列表及详情 | `references/api_references.md` |
| `download_file` | 下载文件 | `references/api_references.md` |

### 管文档（5 个工具）

| 工具名 | 功能 | 参考文档 |
|--------|------|----------|
| `move_file` | 移动文件/文件夹 | `references/api_references.md` |
| `rename_file` | 重命名文件/文件夹 | `references/api_references.md` |
| `share_file` | 开启文档分享 | `references/api_references.md` |
| `set_share_permission` | 设置分享链接权限 | `references/api_references.md` |

### 用文档（4 个工具）

| 工具名 | 功能 | 参考文档 |
|--------|------|----------|
| `read_file_content` | 读取全文转换为 Markdown | `references/api_references.md` |
| `convert_file_content` | 文件格式转换（如 Markdown → DOCX/PDF） | `references/api_references.md` |
| `search_files` | 搜索文档 | `references/api_references.md` |
| `get_file_link` | 获取云文档在线链接 | `references/api_references.md` |

## 各文档类型详细参考

根据操作的文档类型，阅读对应的详细参考文档：

| 文档类型 | 参考文档 | 说明 |
|----------|----------|------|
| 通用 API | `references/api_references.md` | 所有工具的完整参数、示例、返回值 |
| 智能文档 otl | `references/otl_references.md` | 智能文档专属元素操作（页面、文本、标题、待办等） |
| 表格 excel | `references/excel_references.md` | Excel 表格查询、范围数据获取、批量更新 |
| 智能表格 ksheet | `references/ksheet_references.md` | 工作表、视图、字段、记录的增删改查 |
| 文字文档 docx | `references/docx_references.md` | Word 文字文档的创建与内容操作 |
| PDF 文档 pdf | `references/pdf_references.md` | PDF 文档的创建与内容读取 |

## ⚙️ 快速配置

在 OpenClaw 中使用时，需先完成插件安装与 MCP 配置。

> 在 OpenClaw 预置镜像中，如果运行时已经提供 `KDOCS_TOKEN`，bootstrap 会自动执行 `setup.sh` 完成注册。
> 下述步骤主要用于本地手动排障或重新注册。

**安装步骤：**

1. 运行 setup.sh 完成 MCP 服务注册：

```bash
bash setup.sh
```

> setup.sh 会自动将金山文档 MCP 服务注册到 mcporter，并验证配置是否成功。
> 如果未执行 setup，所有工具调用将无法找到 `kdocs` 服务。
> ⚠️ **如果 `KDOCS_TOKEN` 为空、未配置或者登录过期**，请先访问 [https://365.kdocs.cn/latest](https://365.kdocs.cn/latest) 获取 Token，并配置环境变量：`export KDOCS_TOKEN="你的Token值"`，否则所有工具调用将返回鉴权失败。

## 🔧 调用方式

### 获取工具列表

若通过 mcporter 调用，可先列出服务与工具：

```bash
mcporter list kdocs
```

### 调用示例

**详细参数与返回值见 `references/api_references.md`。**

```bash
# 搜索文档
mcporter call "kdocs" "search_files" --args '{"keyword": "区域周报告", "type": "all", "page_size": 20}'

# 读取文档内容
mcporter call "kdocs" "read_file_content" --args '{"file_id": "string"}'

# 创建智能文档
mcporter call "kdocs" "create_file" --args '{"drive_id": "string", "parent_id": "0", "file_type": "file", "name": "周报.otl"}'

# 新建文件夹
mcporter call "kdocs" "create_file" --args '{"drive_id": "string", "parent_id": "0", "file_type": "folder", "name": "项目文档"}'

# 重命名文件（须带后缀）
mcporter call "kdocs" "rename_file" --args '{"drive_id": "string", "file_id": "string", "dst_name": "新名称.otl"}'

# 开启文件分享
mcporter call "kdocs" "share_file" --args '{"drive_id": "string", "file_id": "string", "role_id": "writer", "scope": "anyone"}'

# 网页剪藏
mcporter call "kdocs" "scrape_url" --args '{"url": "https://example.com/article"}'
```

---

## 常见工作流

### 搜索-读取-汇报撰写

**场景**：用户需要搜索多份文档、提取信息、汇总撰写新报告。

> 示例：「去我的金山文档里，找广州、珠海这三个团队本周刚更新的《区域周报告》，拉出来读一遍，提取核心的销售数据和卡点，然后模仿我上周的《南区总监月报》语气，帮我写一份本周的汇报文档」

**流程**：`search_files` → `read_file_content`（多次） → AI 分析汇总 → `create_file` → `upload_file`（传 file_id 写入内容）

```
步骤1: search_files(keyword="区域周报告")
       → 获取匹配的文件列表

步骤2: read_file_content(file_id=各文件ID)
       → 逐个读取文档内容（支持所有格式：otl/docx/excel/pdf）

步骤3: search_files(keyword="南区总监月报")
       → 找到参考文档

步骤4: read_file_content(file_id=参考文档ID)
       → 读取参考文档的语气和格式

步骤5: AI 分析提取核心数据，模仿语气撰写内容

步骤6: create_file(name="本周区域总监周报.otl", file_type="file")
       → 创建新文档

步骤7: upload_file(drive_id=..., parent_id=..., file_id=新文档ID, content_base64=内容 Base64)
       → 通过三步上传流程将内容写入文档

步骤8: get_file_link(file_id=新文档ID) 或 share_file(drive_id=..., file_id=新文档ID, role_id=..., scope="anyone")
       → 获取/分享文档链接
```

---

### 定期读取文档内容与播报

**场景**：定期读取指定文档，提取关键信息并生成摘要，附上编辑链接。

> 示例：「每天早上 9 点，自动读取金山文档里那个"每周汇报"文档的内容，把所有有期限需求和疑难点提出来，汇总成一段 100 字左右的"风险速报"，附上原文档编辑链接」

**流程**：`search_files` → `read_file_content` → AI 摘要 → `get_file_link`

```
步骤1: search_files(keyword="每周汇报")
       → 定位目标文档

步骤2: read_file_content(file_id=目标文档ID)
       → 读取全文内容

步骤3: AI 提取有期限的需求和疑难点，生成 100 字风险速报

步骤4: get_file_link(file_id=目标文档ID)
       → 获取文档在线链接，附在速报末尾
```

---

### 智能分类整理

**场景**：列出指定目录下的所有文件，按内容分类，创建文件夹并移动归类。

> 示例：「帮我整理一下金山文档里【我的云文档】目录下的文件，看看怎么分类合适」

**流程**：`search_files`（定位目录）→ `list_files` → `read_file_content`（批量） → AI 分类 → `create_file`（file_type=folder） → `move_file`

```
步骤1: search_files(keyword="我的云文档", type="file_name", file_type="folder", page_size=20)
       → 搜索定位目标目录，获取 drive_id 和 parent_id
       → 若已知 drive_id，根目录 parent_id 为 "0"

步骤2: list_files(drive_id=驱动盘ID, parent_id=目标目录ID, page_size=100)
       → 获取目录下所有文件列表

步骤3: read_file_content(drive_id=驱动盘ID, file_id=各文件ID, format="markdown")
       → 批量读取文件内容，转为 Markdown 用于分析

步骤4: AI 根据文件名和内容分析，建议分类方案

步骤5: create_file(drive_id=驱动盘ID, parent_id=目标目录ID, file_type="folder", name="合同文件")
       → 创建分类文件夹

步骤6: move_file(drive_id=驱动盘ID, file_ids=[文件ID列表], dst_drive_id=驱动盘ID, dst_parent_id=新文件夹ID)
       → 将文件批量移动到对应分类文件夹
```

---

### 网页剪藏收藏

**场景**：将外部网页链接的内容转为金山文档并存到指定文件夹。

> 示例：「把这个外网长篇研报的链接（https://xxx）转成文档，存到我的文件夹下面」

**流程**：`search_files` → `scrape_url` → `scrape_progress`

```
步骤1: search_files(keyword="目标文件夹名")
       → 搜索确认目标文件夹是否存在，获取 folder_id
       → 如果不存在：create_file(drive_id=..., parent_id=..., file_type="folder", name="目标文件夹名") 创建

步骤2: scrape_url(url="https://xxx")
       → 发起网页剪藏，获取 task_id

步骤3: scrape_progress(task_id=task_id, parent_id=folder_id)
       → 轮询进度（每 2 秒一次），直到 status=2 完成
       → 完成后获取 file_id 和 file_url

步骤4: 返回文档链接给用户
```

---

### 精准搜寻与风险排查

**场景**：在特定文件夹下搜索批量文档，逐一读取分析，提取关键信息汇总到新文档。

> 示例：「去找我金山文档里【待签署】文件夹下的那 5 份《供应商合作协议》都扫一遍，找出所有"付款周期超过 60 天"或者包含"无限连带责任"的条款，汇总到我的《今日合同风险排查台账》文档里」

**流程**：`search_files`（精准定位）→ `read_file_content`（批量读取）→ AI 分析 → `create_file` / `upload_file`

```
步骤1: search_files(keyword="待签署")
       → 定位【待签署】文件夹，获取 parent_id

步骤2: search_files(keyword="供应商合作协议", parent_id=待签署文件夹ID)
       → 精准搜索目标文件

步骤3: read_file_content(file_id=协议1ID)
       read_file_content(file_id=协议2ID)
       ... （逐个读取所有匹配文件）
       → 批量读取全文内容

步骤4: AI 分析每份协议，提取"付款周期超过60天"和"无限连带责任"相关条款

步骤5: search_files(keyword="今日合同风险排查台账")
       → 检查目标汇总文档是否已存在
       → 已存在：upload_file(drive_id=..., parent_id=..., file_id=文档ID, content_base64=...) 更新
      → 不存在：create_file(drive_id=..., parent_id="0", name="今日合同风险排查台账.otl", file_type="file") 新建
                 → upload_file(drive_id=..., parent_id=..., file_id=新文档ID, content_base64=...) 写入内容

步骤6: get_file_link(file_id=台账文档ID)
       → 返回台账文档链接
```

---

### 总结分析与客户反馈拆分

**场景**：搜索特定文档或通过分享链接定位文档，读取全文内容，进行深度分析和总结。

> 示例：「看看这个金山文档里 500 条客户声量聚.docx，帮我遍历所有情绪化的内容，把客户最不满意的三个核心功能按缺陷维度挑出来」

**流程A（通过关键词搜索）**：`search_files` → `read_file_content` → AI 深度分析

**流程B（通过分享链接）**：从链接提取 link_id → `get_share_info` → `get_file_info` → `read_file_content` → AI 深度分析

```
# 流程A：通过关键词搜索
步骤1: search_files(keyword="客户声量", type="all", page_size=20)
       → 定位目标文档，获取 file_id 和 drive_id

步骤2: read_file_content(drive_id=驱动盘ID, file_id=目标文档ID, format="markdown")
       → 读取完整内容

# 流程B：通过分享链接（如 https://365.kdocs.cn/l/cigLc2eDEwOm）
步骤1: 从链接中提取 link_id（链接末尾部分，如 cigLc2eDEwOm）

步骤2: get_share_info(link_id="cigLc2eDEwOm")
       → 获取分享链接信息，得到 file_id 和 drive_id

步骤3: read_file_content(drive_id=驱动盘ID, file_id=文件ID, format="markdown")
       → 读取完整内容

# 后续分析（两种流程共用）
AI 遍历所有内容，识别情绪化表达
       → 分类客户不满情绪
       → 按缺陷维度提取 Top 3 核心功能问题
       → 结构化输出分析结果
```

---

## 工具组合速查

| 用户需求 | 推荐工具组合 |
|----------|-------------|
| 找文档 | `search_files` |
| 找文档 + 读内容 | `search_files` → `read_file_content` |
| 找文档 + 读内容 + 写新文档 | `search_files` → `read_file_content` → `create_file` |
| 找文档 + 读内容 + 更新已有文档 | `search_files` → `read_file_content` → `upload_file`（传 file_id） |
| 浏览目录 | `list_files` 或 `     ` |
| 整理归类文件 | `list_files` → `read_file_content` → `create_file`(folder) → `move_file` |
| 网页保存为文档 | `scrape_url` → `scrape_progress` |
| 分享文档 | `share_file` → `set_share_permission` |
| 获取文档链接 | `get_file_link` |

---

## 问题定位指南

### 常见问题

| 现象 | 可能原因 | 建议 |
|------|----------|------|
| **400006** | **Token 鉴权失败** | 🔑 检查 Token 配置：确认 Header 的 key **必须**使用 `Authorization`；同时确认 Token 值正确，可访问 [https://365.kdocs.cn/latest](https://365.kdocs.cn/latest) 重新获取 |
| 工具调用找不到 | 未执行 setup.sh | 运行 `bash setup.sh` 注册 MCP 服务 |
| 搜索无结果 | 关键词过于精确 | 尝试缩短关键词或分词搜索 |
| 读取内容为空 | 文件无内容或格式不支持 | 确认文件非空且格式受支持 |
| 创建文件失败 | 文件名后缀不正确 | 确认 `name` 带正确后缀：`.otl`/`.docx`/`.xlsx`/`.ksheet`/`.dpdf` |
| 移动文件失败 | 目标文件夹不存在 | 先用 `search_files` 确认或 `create_file`(file_type=folder) 创建 |

### 排查步骤

1. 确认 OpenClaw 中已安装并启用金山文档插件，且已配置 MCP 端点与鉴权。
2. 对照 `references/api_references.md` 核对工具名称、必填参数。
3. 若使用 mcporter，用 `mcporter list kdocs` 确认服务与工具已注册。
4. 若涉及特定文档类型的高级操作，阅读对应的类型参考文档（`references/otl_references.md` 等）。
