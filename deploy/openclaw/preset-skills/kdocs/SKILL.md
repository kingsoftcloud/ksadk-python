---
name: kdocs
description: 金山文档 Skill。通过 MCP 工具与金山文档在线 API 交互，支持智能文档(otl)、Word(docx)、Excel(xlsx)、智能表格(ksheet)、PDF(dpdf) 的创建、读取、编辑、搜索、分享与整理。使用此 Skill 当用户需要操作金山文档云端文件——搜索文档、读取内容、创建/编辑文档、分享文件、整理归类时。
homepage: https://365.kdocs.cn/latest
version: 1.0.0
metadata: {"openclaw":{"category":"kdocs","tokenUrl":"https://365.kdocs.cn/latest","emoji":"📝"}}
---

# 金山文档 Skill 使用指南

金山文档 Skill 提供了一套完整的在线文档操作工具，支持创建、查询、读取、编辑、分享、移动多种类型的在线文档。

## 版本自检

首次使用或间隔超过 24 小时后，需要确认当前 Skill 是否为最新版本。

从本文件头部 frontmatter 的 `version` 字段读取本地版本号（语义化版本 X.Y.Z），然后调用远端接口比对：

```bash
mcporter call "https://mcp-center.wps.cn/skill_hub/mcp" "check_skill_update" --args '{"version": "<本地版本号>"}'
```

接口返回 JSON 结构，包含三个关键字段：

| 字段 | 含义 |
|------|------|
| `latest` | 远端最新版本号 (X.Y.Z) |
| `release_note` | 该版本的变更摘要 |
| `instruction` | 升级操作步骤 |

若本地版本落后于 `latest`，按 `instruction` 中的指引完成升级或者把最新版信息用户告知用户。

## 认证配置

### Token 获取与管理

| 步骤 | 操作 |
|------|------|
| 读取 | 优先从环境变量 `KINGSOFT_DOCS_TOKEN` 中读取，若已存在且有效则直接使用 |
| 获取 | 若环境变量为空或 Token 过期（错误码 `400006`），优先运行 `bash get-token.sh` 获取新 Token；也可访问 [https://365.kdocs.cn/latest](https://365.kdocs.cn/latest) 登录后手动获取 |
| 配置 | `export KINGSOFT_DOCS_TOKEN="你的Token值"` |
| 验证 | 调用任意读取工具（如 `search_files`），返回 `code: 0` 即认证成功 |
| 过期 | 收到错误码 `400006` 时，Token 已过期，按上述「获取」步骤重新获取 |

> ⚠️ **KINGSOFT_DOCS_TOKEN 为空或过期时，所有工具调用将返回鉴权失败（400006）。**
> 🔒 **Token 安全**：任何时候都不得将 Token 明文值展示给用户或拼接到命令中。需要引用时只使用 `$KINGSOFT_DOCS_TOKEN`。

### drive_id 获取流程

多数写/管工具需要 `drive_id` 参数。获取方式：

1. **首选**：`search_files` 搜索目标文件/文件夹 → 返回结果中包含 `drive_id`
2. **已知 file_id**：`get_file_info(file_id=xxx)` → 返回中取 `drive_id`
3. **已知分享链接**：从链接路径（`/l/`、`/folder/`、`/view/l/`）提取末尾的 `link_id` → `get_share_info(link_id=xxx)` → 取 `drive_id` 和 `file_id`
4. **创建文件**：用户指定了目标目录 → `search_files` 搜索该目录获取其 `drive_id` 和 `file_id`（作为 `parent_id`）；用户未指定目录 → 使用根目录（`parent_id` = `"0"`），通过 `list_files(parent_id="0")` 获取 `drive_id`

> 根目录的 `parent_id` 固定为 `"0"`。

### 环境配置

**OpenClaw**：运行 `bash setup.sh` 注册 MCP 服务到 mcporter，首次使用或token过期时，运行`bash get-token.sh`获取新的Token。

**Cursor / Claude Code 等客户端**：在 MCP 配置中添加金山文档服务，确保 `KINGSOFT_DOCS_TOKEN` 已设置。

---

## 操作限制

以下为具体可判定的约束，违反将导致调用失败或数据异常：

1. **文件名必须带后缀**：`create_file` 和 `rename_file` 时，`name` / `dst_name` 必须含正确后缀（`.otl` / `.docx` / `.xlsx` / `.ksheet` / `.pdf`），文件夹不需要后缀
2. **upload_file 仅更新已有文件**：必须传 `file_id`，不能用于新建；新建用 `create_file`。**PDF 不支持 create_file 创建，仅能通过 upload_file 上传。**
3. **move_file 是异步操作**：调用成功只表示任务提交，需验证实际结果
4. **cancel_share 的 delete 模式不可逆**：`mode=delete` 永久删除分享链接，默认 `mode=pause` 仅暂停
5. **search_files 有延迟**：新建文件后搜索可能无法立即命中，需等待索引更新
6. **禁止泄露凭据**：不得将 KINGSOFT_DOCS_TOKEN 的值以明文形式出现在对话、日志、命令输出、代码注释或任何文件中。执行 shell 命令时必须使用变量引用（`$KINGSOFT_DOCS_TOKEN`），禁止将 Token 值直接拼接到命令字符串里
7. **工具调用**: 当工具名包含 `.` 时，务必使用引号包裹工具名以确保正常调用（例如 `"otl.insert_content"`）

---

## 能力范围

### 支持的文档类型

| 类型 | 别名 | 文件后缀 | 推荐度 | 说明 |
|------|------|----------|--------|------|
| **智能文档** | ap | .otl | [===] **首选** | 排版美观，支持丰富组件 |
| 表格 | et | .xlsx | [===] | 数据表格专用 |
| PDF文档 | pdf | .pdf | [===] | PDF 文档专用 |
| 文字文档 | wps | .docx | [===] | 传统格式 |
| 智能表格 | as | .ksheet | [===] | 结构化表格，支持多视图、字段管理 |

> **别名识别**：用户提到 `ap文件` = 智能文档(.otl)、`et文件` = 表格(.xlsx)、`wps文件` = 文字文档(.docx)、`as文件` = 智能表格(.ksheet)。遇到这些别名时，自动映射到对应的文档类型进行操作。

### 文档类型选择

```
用户需要创建文档
├── 需要丰富排版/图文混排？ → otl（智能文档）首选
├── 需要表格/数据处理？
│   ├── 简单表格数据 → xlsx
│   └── 需要多视图/字段管理/看板 → ksheet（智能表格）
├── 需要生成 PDF？ → dpdf
├── 需要兼容 Word？ → docx
└── 不确定 → otl（智能文档）默认推荐
```

### 工具总览

| 类别          | 工具名                    | 功能 |
|-------------|------------------------|------|
| **写**       | `create_file`          | 创建文件/文件夹 |
| **写**       | `upload_file`          | 更新已有文件内容（传 file_id + content_base64） |
| **写**       | `scrape_url`           | 网页剪藏 |
| **写**       | `scrape_progress`      | 查询剪藏进度 |
| **读**       | `list_files`           | 列出目录内容 |
| **读**       | `download_file`        | 下载文件 |
| **读**       | `get_file_info`        | 获取文件信息（含 drive_id） |
| **管**       | `move_file`            | 移动文件/文件夹 |
| **管**       | `rename_file`          | 重命名 |
| **管**       | `share_file`           | 开启分享 |
| **管**       | `set_share_permission` | 设置分享权限 |
| **管**       | `cancel_share`         | 取消分享 |
| **管**       | `get_share_info`       | 获取分享信息 |
| **用**       | `read_file_content`    | 读取文档转 Markdown |
| **用**       | `search_files`         | 搜索文档 |
| **用**       | `get_file_link`        | 获取在线链接 |
| **表格类**     | `sheet.*`              | 表格文档操作                               |
| **智能文档类**   | `otl.*`                | 智能文档操作                               |

### 不支持的操作

- 无批量删除文件工具（仅支持移动）
- 无文件权限精细管控（仅支持分享链接级别）
- 无文件版本回滚
- 无实时协同编辑控制

完整参数、示例与返回值见 `references/api_references.md`。

---

## 核心操作摘要

### 创建并写入文档（两步流程）

```
步骤1: create_file(drive_id, parent_id, name="xxx.otl", file_type="file")
       → 获取新文件的 file_id
步骤2: upload_file(drive_id, parent_id, file_id=新文件ID, content_base64=Base64编码内容)
       → 写入内容（支持 content_format="markdown" 自动转换）
```

### 读取文档内容

- **Excel（.xlsx）**：使用 `sheets_list_worksheets` → `sheets_get_range_data` 原子化工具读取，**不使用 `read_file_content`**。详见 `references/sheet_references.md`
- **其他类型**（.otl / .docx / .ksheet / .pdf）：使用 `read_file_content(file_id)` 读取

### 搜索定位文档

```
search_files(keyword="关键词", type="all", page_size=20)
→ 返回匹配文件列表，每项含 file_id、drive_id、name
```

`type` 可选值：`all`（全部）、`file_name`（仅文件名）、`content`（全文）

### 通过分享链接定位

用户发来的链接可能包含以下三种路径格式，均需提取末尾的 `link_id`：

| URL 路径格式 | 目标类型 | 示例 | 提取的 link_id |
|-------------|---------|------|---------------|
| `/l/<link_id>` | 文件 | `https://365.kdocs.cn/l/cigLc2eDEwOm` | `cigLc2eDEwOm` |
| `/folder/<link_id>` | 文件夹 | `https://365.kdocs.cn/folder/abc123XYZ` | `abc123XYZ` |
| `/view/l/<link_id>` | 文件 | `https://365.kdocs.cn/view/l/defGHI456` | `defGHI456` |

> ⚠️ **识别规则**：当用户发送的链接路径匹配 `/l/`、`/folder/` 或 `/view/l/` 时，应自动提取路径末尾的 `link_id`，然后调用 `get_share_info` 获取文件信息。

```
提取 link_id → get_share_info(link_id="xxx") → 获得 file_id 和 drive_id
```

### 网页剪藏

> 🎯 **当用户要求保存网页/URL 到金山文档时，直接调用 `scrape_url`。禁止先用 `web_fetch`、`web_search` 或浏览器抓取内容。**

**触发识别**：用户消息中同时包含 **URL**（非金山文档链接）+ **保存/存到/收藏/剪藏** 等意图词时，走此流程。

```
步骤 1: scrape_url(url="https://example.com")
        → 返回 jobID

步骤 2: scrape_progress(jobID=xxx)
        → 轮询（每 2-5 秒），直到 status=1（完成）
        → 获得 fileID → get_file_link 返回文档链接
```

| status | 含义 | 操作 |
|--------|------|------|
| 1 | 完成 | 获得 fileID，调用 get_file_link 返回文件链接 |
| -1 | 失败 | 检查 URL 或重试 |
| 其他 | 进行中 | 继续轮询 |

---

## 操作守护规则

### 操作前检查

| 操作类型 | 执行前必须确认 |
|----------|---------------|
| 创建文件 | `search_files` 检查同名文件是否已存在 |
| 写入/更新 | `read_file_content` 读取现有内容，确认不会误覆盖 |
| 移动文件 | `get_file_info` 确认目标文件夹存在且 ID 正确 |
| 重命名 | `get_file_info` 确认文件存在 |
| 删除分享 | `get_share_info` 确认分享信息，**向用户确认意图** |

### 交付验证

> **原则：不信任操作返回的 `code: 0`。用独立的读取请求验证实际结果。**

| 操作 | 验证方式 | 通过条件 |
|------|----------|----------|
| `create_file` | `get_file_info(file_id=返回的id)` | 能读到且名称正确 |
| `upload_file` | `read_file_content(file_id=xxx)` | 内容与写入一致 |
| `move_file` | `get_file_info(file_id=xxx)` | `parent_id` 为目标文件夹 |
| `rename_file` | `get_file_info(file_id=xxx)` | `name` 为新名称 |
| `share_file` | `get_share_info(link_id=返回的link_id)` | 权限与设置一致 |
| `cancel_share` | `get_share_info(link_id=xxx)` | 状态已变更 |
| `scrape_url` | `scrape_progress` 轮询至 `status=1` | 获得 `fileID` |

### 不可逆操作保护

| 操作 | 风险 | 安全措施 |
|------|------|----------|
| `move_file` | 文件移出原目录 | 执行前记录原 `parent_id`，告知用户可移回 |
| `cancel_share(mode=delete)` | 分享链接永久删除 | **必须**向用户确认；建议优先用 `mode=pause` |
| `upload_file` | 覆盖已有内容 | 先 `read_file_content` 备份原内容摘要 |

### 幂等性与重试

| 操作 | 幂等 | 重试策略 |
|------|------|----------|
| 所有读取操作 | ✅ | 可安全重试 |
| `create_file` | ❌ | 重试前 `search_files` 检查是否已创建 |
| `upload_file` | ✅ | 可重试，以最后一次为准 |
| `move_file` / `rename_file` / `share_file` | ✅ | 可重试 |
| `cancel_share(pause)` | ✅ | 可重试 |
| `cancel_share(delete)` | ❌ | **禁止重试** |
| `scrape_url` | ❌ | 重试前查 `scrape_progress` 确认上次状态 |

### 错误速查表

| 错误特征 | 原因 | 处理方式 |
|----------|------|----------|
| `400006` / 鉴权失败 | Token 过期或未配置 | 提示用户重新获取 Token |
| 工具找不到 | 未注册 MCP 服务 | OpenClaw: `bash setup.sh`；其他客户端: 检查 MCP 配置 |
| 搜索无结果 | 关键词过精确 / 索引延迟 | 缩短关键词 / 等待 3-5 秒重试 |
| 读取内容为空 | 文件无内容或格式不支持 | 确认文件非空且后缀正确 |
| 创建文件失败 | 文件名后缀不正确 | 检查后缀：`.otl` / `.docx` / `.xlsx` / `.ksheet` / `.pdf` |
| 移动文件失败 | 目标文件夹不存在 | 先搜索确认或创建文件夹 |
| HTTP 5xx / 超时 | 服务端故障 | 等 3 秒重试 1 次 |
| 验证不通过（回读值与预期不符） | 写入未生效或延迟 | 等 2 秒重新验证，仍不通过则报告用户 |

---

## 常见工作流

### 搜索-读取-汇报撰写

`search_files` → `read_file_content`（多次）→ AI 分析 → `create_file` → `upload_file` → `get_file_link`

> 场景：搜索多份文档、提取信息、汇总撰写新报告

### 定期读取与播报

`search_files` → `read_file_content` → AI 摘要 → `get_file_link`

> 场景：定期读取指定文档，提取关键信息生成摘要

### 智能分类整理

`search_files` → `list_files` → `read_file_content`（批量）→ AI 分类 → `create_file(folder)` → `move_file`

> 场景：列出目录，按内容分类创建文件夹并归档。⚠️ `move_file` 前需向用户确认分类方案

### 网页剪藏收藏

`scrape_url` → `scrape_progress`（轮询至完成）→ `get_file_link`

> 场景：将网页内容保存为金山文档

### 精准搜索与风险排查

`search_files`（定位目录）→ `search_files`（精确搜索）→ `read_file_content`（批量）→ AI 分析 → `create_file` + `upload_file`

> 场景：在特定目录批量搜索文档，逐一读取分析，汇总到新文档

### 分享链接定位与分析

从链接提取 `link_id` → `get_share_info` → `read_file_content` → AI 分析

> 场景：通过分享链接定位并分析文档。支持 `/l/xxx`、`/folder/xxx`、`/view/l/xxx` 三种链接格式

---

## 工具组合速查

| 用户需求 | 推荐工具组合 |
|----------|-------------|
| 找文档 | `search_files` |
| 找 + 读 | `search_files` → `read_file_content` |
| 找 + 读 + 写新 | `search_files` → `read_file_content` → `create_file` → `upload_file` |
| 找 + 读 + 更新 | `search_files` → `read_file_content` → `upload_file`（传 file_id） |
| 浏览目录 | `list_files` |
| 整理归类 | `list_files` → `read_file_content` → `create_file(folder)` → `move_file` |
| 网页保存 | `scrape_url` → `scrape_progress` |
| 分享文档 | `share_file` → `set_share_permission` |
| 获取链接 | `get_file_link` |

---

## 各文档类型详细参考

| 文档类型 | 参考文档 | 说明 |
|----------|----------|------|
| 通用 API | `references/api_references.md` | 所有工具完整参数、示例、返回值 |
| 智能文档 otl | `references/otl_references.md` | 页面、文本、标题、待办等元素操作 |
| 表格（Excel & 智能表格） | `references/sheet_references.md` | 工作表管理、范围数据获取、批量更新（同时适用于 .xlsx 和 .ksheet） |
| 文字文档 docx | `references/docx_references.md` | Word 文档创建与内容操作 |
| PDF 文档 pdf | `references/pdf_references.md` | PDF 创建与内容读取 |
