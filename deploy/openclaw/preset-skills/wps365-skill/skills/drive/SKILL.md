---
name: wps365-drive
description: 云文档管理：上传、更新、Markdown 写入、列表、详情、下载、正文抽取、搜索、分享链接信息。单文件 .md 自动上传为智能文档。调用 V7 Drive 接口。
---

# 云文档·Drive（V7）

## 何时使用

- 需要做云文档**基础文件流转**：创建、上传、更新、列表、详情、下载（支持 `file_id` / `link_id` 混用）
- 需要做云盘内**文件管理操作**：新建文件(夹)、复制、移动、重命名、另存为、重名检查
- 需要做文档**内容处理**：抽取正文（`read/extract`，支持 plain/markdown/html/kdc）或把 Markdown 写回文档（`write`）
- 需要做文档**检索与发现**：普通搜索（`search`）、最近文档（`latest`）
- 需要做文档**组织管理**：收藏（列表/增删）、标签（列表/创建/绑定对象/解绑对象）
- 需要处理**回收站流程**：回收站列表、回收站还原
- 需要处理**分享链路**：开启/关闭分享链接，或通过 `link-meta` 将 `link_id` 解析为 `file_id/drive_id`

## 前置条件

- 已设置环境变量 `wps_sid`（或 `WPS_SID`）
- 在 `wps365-skill` 根目录执行命令

## 能力概览

| 操作 | 说明 | 常用参数 |
|------|------|----------|
| **创建** | 在云盘下创建文件（支持 多维表.otl、智能文档.dbt 等）| `file_name`（含后缀），`--path`，`--drive`，`--on-conflict` |
| 上传 | 上传本地文件到云端；**单文件 .md 时自动走 Airpage 上传为智能文档** | `file_path`，`--path`（目标路径），`--filename`（云端文件名）|
| **更新** | 更新现有文件（上传本地文件新版本覆盖）| `file_id`，`file_path`，`--drive` |
| **写入** | 将 Markdown 写入文档：智能文档插入，文字/PDF 转换后覆盖 | `file_id`，`--content`/`--file`，`--drive`，`--mode`，`--title`/`--template` |
| 列表 | 获取目录文件列表 | `--drive`，`--parent`，`--page-size` / `--page-token` / `--all` |
| 详情 | 获取文件详情（支持 file_id 或 link_id） | `file_id\|link_id`，`--drive` |
| 下载 | 获取文件下载链接 | `file_id\|link_id`，`--drive` |
| **抽取** | 按云文档抽取正文，internal GET .../content | `file_id\|link_id`，`--format`（plain/markdown/html/kdc），`--drive` |
| **搜索** | 搜索云文档文件，GET /v7/files/search | `keyword`，`--type`，`--scope`，`--page-size` / `--page-token` |
| **最近列表** | 获取用户最近打开编辑的文档，GET /v7/drive_latest/items | `--page-size` / `--page-token` |
| **收藏列表** | 获取用户收藏文档，GET /v7/drive_star/items | `--page-size` / `--page-token`，`--order`，`--order-by` |
| **批量添加收藏项** | 批量添加收藏项，POST /v7/drive_star/items/batch_create | `--objects` / `--objects-json` |
| **批量移除收藏项** | 批量移除收藏项，POST /v7/drive_star/items/batch_delete | `--objects` / `--objects-json` / `--item-ids` |
| **自定义标签列表** | 分页获取自定义标签列表，GET /v7/drive_labels | `--allotee-type`，`--label-type`，`--page-size` / `--page-token` |
| **标签详情** | 获取单个标签信息，GET /v7/drive_labels/{label_id}/meta | `label_id` |
| **标签对象列表** | 分页获取标签下对象，GET /v7/drive_labels/{label_id}/objects | `label_id`，`--page-size`，`--page-token`，`--file-type` |
| **创建标签** | 创建自定义标签，POST /v7/drive_labels/create | `--name`，`--allotee-type`，`--label-type` |
| **批量添加标签对象** | 批量添加标签对象，POST /v7/drive_labels/{label_id}/objects/batch_add | `label_id`，`--objects` / `--objects-json` |
| **批量移除标签对象** | 批量移除标签对象，POST /v7/drive_labels/{label_id}/objects/batch_remove | `label_id`，`--objects` / `--objects-json` |
| **回收站列表** | 获取回收站文件列表，GET /v7/deleted_files | `--drive-id`，`--page-size` / `--page-token` |
| **回收站还原** | 还原回收站文件，POST /v7/deleted_files/{file_id}/restore | `file_id` |
| **移动文件** | 移动文件，POST /v7/drives/{drive_id}/files/{file_id}/move | `drive_id`，`file_id`，`--dst-drive-id`，`--dst-parent-id` |
| **复制文件** | 复制文件，POST /v7/drives/{drive_id}/files/{file_id}/copy | `drive_id`，`file_id`，`--dst-drive-id`，`--dst-parent-id` |
| **重命名文件** | 重命名文件（夹），POST /v7/drives/{drive_id}/files/{file_id}/rename | `drive_id`，`file_id`，`--dst-name` |
| **文件另存为** | 文件另存为，POST /v7/drives/{drive_id}/files/{file_id}/save_as | `drive_id`，`file_id`，`--dst-drive-id`，`--dst-parent-id` |
| **文件名检查** | 检查文件名是否存在，POST /v7/drives/{drive_id}/files/{parent_id}/check_name | `drive_id`，`parent_id`，`--name` |
| **新建文件(夹)** | 统一用 create 新建文件（夹），POST /v7/drives/{drive_id}/files/{parent_id}/create | `create <file_name>`，`--file-type`，`--parent-id` |
| **开启/取消分享** | 开启或取消文件分享，POST /open_link /close_link | `drive_id`，`file_id` |
| Link 详情 | 根据 link_id 获取分享链接信息（含 file_id、drive_id） | `link_id` |

> **云盘ID**：`private`（我的云文档）、`roaming`（漫游箱）、`special`（团队云文档）

## 使用方式

在 `wps365-skill` 根目录执行：
```bash
python skills/drive/run.py <子命令> [参数...]
```

## run.py 子命令

- **create** `<file_name>` — 在云盘下创建文件（支持 .otl、.dbt 等）
  - `--drive/-d`：云盘（private/roaming/special），默认 private
  - `--path/-p`：父路径，如 `我的文档` 或 `我的文档/子目录`，默认 我的文档
  - `--on-conflict`：同名时 rename（重命名）或 overwrite（覆盖），默认 rename
- **upload** `[文件路径]` — 上传文件到云端；若为**单个 .md 文件**则自动走 Airpage 上传为**智能文档**（.otl）
  - `--drive/-d`：云盘ID（private/roaming/special），默认 private
  - `--parent/-p`：父目录ID，默认 root（仅普通上传生效）
  - `--path`：目标路径，如 `我的文档/子目录`；.md 时对应智能文档父路径
  - `--filename/-n`：云端文件名（默认与本地相同）
- **update** `<file_id|link_id> <file_path>` — 更新现有文件（上传本地文件新版本覆盖）
  - `--drive/-d`：云盘ID，默认 private
- **read** / **extract** `<file_id|link_id>` — 读取文档内容为 Markdown/正文（read 为 extract 的别名）
  - 支持类型：智能文档（.otl）、文字文档（.docx）、PDF 文档（.pdf）
  - `--format/-f`：输出格式 plain | markdown | html | kdc，默认 **markdown**（优先输出 markdown）
  - `--drive/-d`：云盘ID，默认 private
  - `--raw/-r`：仅输出正文，无 Markdown 包装与代码块
  - `--json`：仅输出 JSON（含 file_id、file_name、file_type、format、content）
  - `--type/-t`：文档类型（可选，默认按扩展名检测）doc | ap | pdf | ppt
- **write** `<file_id|link_id>` — 将 Markdown 写入已有文档（与 update 并列：update 用本地文件覆盖，write 用 Markdown 内容）
  - 支持类型：智能文档（.otl）、文字文档（.docx）、PDF 文档（.pdf）
  - 智能文档：insertContent 插入，支持 `--mode` overwrite（从头）| append（追加）
  - 文字/PDF：Markdown 转 DOCX/PDF 后覆盖上传，生成新版本
  - `--content/-c`：要写入的 Markdown 内容
  - `--file/-f`：从本地文件读取 Markdown
  - `--title`：文档标题（智能文档时使用）
  - `--template`：DOCX 模板文件路径（文字文档转换时使用）
  - `--mode/-m`：写入模式 overwrite | append，默认 overwrite
  - `--drive/-d`：云盘ID，默认 private
  - `--json`：仅输出 JSON
- **list** — 文件列表
  - `--drive/-d`：云盘ID，默认 private
  - `--parent/-p`：目录ID，默认 root
  - `--page-size/-s`：分页大小，默认 50
  - `--page-token`：分页 token（上一页返回的 next_page_token）
  - `--all`：拉取全部分页
- **get** `<file_id|link_id>` — 文件详情（传 link_id 时自动解析为 file_id）
  - `--drive/-d`：云盘ID（仅传入 file_id 时生效）
- **download** `<file_id|link_id>` — 文件下载链接（同上）
- **search** `[关键词]` — 搜索云文档文件
  - `--type/-t`：file_name | content | all，默认 all
  - `--scope/-s`：范围，逗号分隔（如 all, personal_drive, group_drive, latest, share_by_me, share_to_me, recycle）
  - `--page-size`：每页条数，默认 20
  - `--page-token`：分页 token
  - `--no-total`：不返回总条数
- **latest** — 获取最近列表（GET /v7/drive_latest/items）
  - `--page-size`：每页条数，默认 50，最大 500
  - `--page-token`：分页 token
  - `--with-permission`：返回文件操作权限
  - `--with-link`：返回分享信息
  - `--include-exts` / `--exclude-exts`：按后缀过滤（逗号分隔）
  - `--include-creators` / `--exclude-creators`：按创建者过滤（逗号分隔）
- **star**（别名 `favorites`）— 获取收藏列表（GET /v7/drive_star/items）
  - `--page-size`：每页条数，默认 50，最大 200
  - `--page-token`：分页 token
  - `--order`：排序方向，`desc` / `asc`
  - `--order-by`：排序字段（如 `ctime` / `file_mtime` / `source` / `fname` / `fsize`）
  - `--with-permission` / `--with-link`：返回权限与分享信息
  - `--include-exts` / `--exclude-exts`：按后缀过滤（逗号分隔）
- **star-add-items** — 批量添加收藏项
  - 接口：`POST /v7/drive_star/items/batch_create`
  - `--objects`：对象 ID 列表（逗号分隔）
  - `--objects-json`：对象数组 JSON（推荐）
  - `--items-json`：兼容旧字段 items 的数组 JSON
- **star-remove-items** — 批量移除收藏项
  - 接口：`POST /v7/drive_star/items/batch_delete`
  - `--objects`：对象 ID 列表（逗号分隔）
  - `--objects-json`：对象数组 JSON（推荐）
  - `--item-ids`：兼容旧字段 item_ids（逗号分隔）
- **tags**（别名 `user-tags`）— 分页获取自定义标签列表（v7）
  - 接口：`GET /v7/drive_labels`
  - `--allotee-type`：标签归属类型，`user` / `group` / `app`（默认 `user`）
  - `--allotee-id`：标签归属 ID（`user` 场景通常可不传）
  - `--label-type`：标签类型，`custom` / `system`（默认 `custom`）
  - `--page-size`：分页大小，默认 20，最大 500
  - `--page-token`：分页 token
- **tag-get** `<label_id>` — 获取单个标签信息
  - 接口：`GET /v7/drive_labels/{label_id}/meta`
- **tag-objects** `<label_id>` — 分页获取标签下对象
  - 接口：`GET /v7/drive_labels/{label_id}/objects`
  - `--page-size`：分页大小，默认 20，最大 100
  - `--page-token`：分页 token
  - `--include-exts` / `--exclude-exts`：按后缀过滤（逗号分隔）
  - `--file-type`：对象类型，`file` / `folder` / `short_cut`（默认 `file`）
  - 默认会自动将对象 `id` 解析为文件名与链接；如需关闭可加 `--no-resolve-meta`
- **tag-create** — 创建自定义标签
  - 接口：`POST /v7/drive_labels/create`
  - `--name`：标签名称（必填）
  - `--allotee-type` / `--allotee-id`：标签归属
  - `--label-type`：`custom` / `system`
  - `--attr`：标签自定义属性（可选）
  - `--rank`：标签排序值（可选）
- **tag-add-objects** `<label_id>` — 批量添加标签对象
  - 接口：`POST /v7/drive_labels/{label_id}/objects/batch_add`
  - `--objects`：对象 ID 列表（逗号分隔）
  - `--objects-json`：完整对象数组 JSON（优先用于复杂场景）
- **tag-remove-objects** `<label_id>` — 批量移除标签对象
  - 接口：`POST /v7/drive_labels/{label_id}/objects/batch_remove`
  - `--objects`：对象 ID 列表（逗号分隔）
  - `--objects-json`：完整对象数组 JSON（优先用于复杂场景）
- **deleted-list** — 获取回收站文件列表
  - 接口：`GET /v7/deleted_files`
  - `--drive-id`：按云盘过滤（可选）
  - `--with-ext-attrs` / `--with-drive`：返回扩展信息
  - `--page-size` / `--page-token`：分页
- **deleted-restore** `<file_id>` — 还原回收站文件
  - 接口：`POST /v7/deleted_files/{file_id}/restore`
- **file-move** `<drive_id> <file_id>` — 移动文件
  - 接口：`POST /v7/drives/{drive_id}/files/{file_id}/move`
  - `--dst-drive-id` / `--dst-parent-id`：目标位置（必填）
  - `--secure-type`：`decrypt` / `encrypt`（可选）
- **file-copy** `<drive_id> <file_id>` — 复制文件
  - 接口：`POST /v7/drives/{drive_id}/files/{file_id}/copy`
  - `--dst-drive-id` / `--dst-parent-id`：目标位置（必填）
  - `--secure-type`：`decrypt` / `encrypt`（可选）
- **file-rapid-upload** `<drive_id> <parent_id>` — 文件秒传（支持覆盖更新）
  - 接口：`POST /v7/drives/{drive_id}/files/{parent_id}/rapid_upload`
  - 必填：`--size` + `--sha256`（或 `--hashes-json`）
  - 覆盖更新：传 `--file-id <目标文件ID>`
  - 可选：`--name`、`--on-name-conflict`、`--on-parent-path-conflict`、`--parent-path`、`--proof-json`
  - 高级场景可直接用：`--body-json '<完整请求体>'`
- **file-rename** `<drive_id> <file_id>` — 重命名文件（夹）
  - 接口：`POST /v7/drives/{drive_id}/files/{file_id}/rename`
  - `--dst-name`：新名称（必填）
- **file-save-as** `<drive_id> <file_id>` — 文件另存为
  - 接口：`POST /v7/drives/{drive_id}/files/{file_id}/save_as`
  - `--dst-drive-id` / `--dst-parent-id`：目标位置（必填）
  - `--name`：目标文件名（可选）
  - `--on-name-conflict`：`fail` / `rename` / `overwrite` / `replace`
- **file-check-name** `<drive_id> <parent_id>` — 检查文件名是否存在
  - 接口：`POST /v7/drives/{drive_id}/files/{parent_id}/check_name`
  - `--name`：待检查名称（必填）
- **create** `<file_name>` — 统一新建文件（夹）/快捷方式
  - 接口：`POST /v7/drives/{drive_id}/files/{parent_id}/create`
  - `--file-type`：`folder` / `file` / `shortcut`
  - `--parent-id`：父目录 ID（默认 `0`）
  - `--file-id`：快捷方式引用文件 ID（`shortcut` 时可用）
  - `--on-conflict`：重名处理策略（`fail` / `rename` / `overwrite` / `replace`，默认 `rename`）
  - `--path`：父路径
- **file-open-link** `<drive_id> <file_id>` — 开启文件分享
  - 接口：`POST /v7/drives/{drive_id}/files/{file_id}/open_link`
  - `--role-id` / `--scope`：权限相关参数（可选）
  - `--opts-json`：完整 opts JSON 对象（可选）
- **file-close-link** `<drive_id> <file_id>` — 取消文件分享
  - 接口：`POST /v7/drives/{drive_id}/files/{file_id}/close_link`
  - `--mode`：`pause` / `delete`，默认 `pause`
- **link-meta** `<link_id>` — 根据 link_id 获取分享链接信息（含 file_id、drive_id），GET /v7/links/{link_id}/meta

### 读取/写入 支持类型与方式

| 操作 | 说明 | 支持类型 | 写入方式 | 常用参数 |
|------|------|----------|----------|----------|
| **读取**（read/extract） | 抽取文档内容为 Markdown/正文 | 智能文档（.otl）<br>文字文档（.docx）<br>PDF 文档（.pdf） | - | `file_id`，`--format`（默认 markdown） |
| **写入**（write） | 将 Markdown 写入已有文档 | 智能文档（.otl）<br>文字文档（.docx）<br>PDF 文档（.pdf） | 智能文档：插入内容<br>文字/PDF：转换+覆盖 | `file_id`，`--content`/`--file` |

> **写入方式说明**：
> - **智能文档（.otl）**：使用内容插入 API（insertContent），在文档中增量插入，支持 begin/end 位置，保留原内容
> - **文字文档（.docx）**：调用转换 API（Markdown → DOCX），再覆盖上传原文件，完全替换内容，生成新版本
> - **PDF 文档（.pdf）**：调用转换 API（Markdown → PDF），再覆盖上传原文件，完全替换内容，生成新版本
>
> **API 限制**：
> - 读取：演示文档（.pptx）暂不支持 Markdown 抽取（返回 400008018）
> - 写入：不能用 .md 文件直接覆盖 .otl 文件（格式验证失败，返回 400000004）

## 使用示例（read / write）

### 读取文档为 Markdown

```bash
python skills/drive/run.py read <file_id>
# 或
python skills/drive/run.py extract <file_id> --format markdown
```

### 写入智能文档（插入内容）

```bash
python skills/drive/run.py write <file_id> --content "# 新内容

## 章节

这是新增的内容。"
```

### 写入文字文档（转换+覆盖）

```bash
# 基本用法
python skills/drive/run.py write <file_id> --content "# 更新的标题

更新的内容..."

# 从文件读取
python skills/drive/run.py write <file_id> --file /path/to/content.md

# 使用模板
python skills/drive/run.py write <file_id> --file content.md --template template.docx
```

### 写入 PDF（转换+覆盖）

```bash
python skills/drive/run.py write <file_id> --content "# PDF 标题

## 章节一

PDF 内容会完全替换原文件。"

# 从文件读取
python skills/drive/run.py write <file_id> --file report.md
```

## 使用示例（其他能力）

### 创建 / 上传 / 更新

```bash
# 创建云文档（.dbt/.otl 等）
python skills/drive/run.py create 反馈管理.dbt
python skills/drive/run.py create 文档.otl --path "我的文档/子目录" --drive private --on-conflict rename

# 上传（单个 .md 自动按智能文档上传）
python skills/drive/run.py upload /path/to/file.md
python skills/drive/run.py upload /path/to/file.docx --drive private --path "我的文档/项目A"

# 更新已有文件（覆盖为新版本）
python skills/drive/run.py update <file_id|link_id> /path/to/new-version.docx
```

### 列表 / 详情 / 下载

```bash
python skills/drive/run.py list --drive private --parent root --page-size 20
python skills/drive/run.py get <file_id|link_id>
python skills/drive/run.py download <file_id|link_id>
```

### 搜索 / 最近 / 链接解析

```bash
# 普通搜索
python skills/drive/run.py search "季度复盘" --type all --scope all --page-size 20

# 最近文档
python skills/drive/run.py latest --page-size 20 --with-link

# link_id 解析为 file_id/drive_id
python skills/drive/run.py link-meta <link_id>
```

### 文件管理（新建/复制/移动/重命名/另存为）

```bash
# 新建文件夹 / 文件（统一 create）
python skills/drive/run.py create "项目资料" --drive <drive_id> --parent-id <parent_id> --file-type folder
python skills/drive/run.py create "需求文档.docx" --drive <drive_id> --parent-id <parent_id> --file-type file

# 文件操作
python skills/drive/run.py file-copy <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <dst_parent_id>
python skills/drive/run.py file-move <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <dst_parent_id>
python skills/drive/run.py file-rename <drive_id> <file_id> --dst-name "需求文档-已评审.docx"
python skills/drive/run.py file-save-as <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <dst_parent_id> --name "副本.docx"
```

### 收藏 / 标签管理

```bash
# 收藏
python skills/drive/run.py star --page-size 20
python skills/drive/run.py star-add-items --objects "<file_id1>,<file_id2>"
python skills/drive/run.py star-remove-items --objects "<file_id1>"

# 标签
python skills/drive/run.py tags --label-type custom --page-size 20
python skills/drive/run.py tag-create --name "重点项目"
python skills/drive/run.py tag-add-objects <label_id> --objects "<file_id1>,<file_id2>"
python skills/drive/run.py tag-remove-objects <label_id> --objects "<file_id2>"
```

### 回收站管理

```bash
python skills/drive/run.py deleted-list --page-size 20
python skills/drive/run.py deleted-restore <file_id>
```

### 分享开关

```bash
# 开启分享
python skills/drive/run.py file-open-link <drive_id> <file_id> --scope anyone

# 取消分享（默认 pause，也可 delete）
python skills/drive/run.py file-close-link <drive_id> <file_id> --mode pause
```

## 输出格式

先输出 Markdown 摘要；多数子命令会再输出完整 JSON（`## 原始数据 (JSON)`）。**extract/read** 默认只输出正文摘要（参考 md-io，不附带 JSON）；加 `--json` 时仅输出 JSON，加 `--raw` 时仅输出正文。

## 发送云文档消息

上传或 get 返回的 JSON 中通常含 `link_id`、`link_url`，可用于 IM 发送云文档消息。`cloud.id` 需为 **link_id**（若只填 link_id 不填 id，IM 的 run.py 会自动用 link_id 填充 id）。

```bash
python skills/im/run.py send <chat_id> --type file --file '{"type":"cloud","cloud":{"link_url":"https://kdocs.cn/l/xxx","link_id":"xxx"}}'
```

## 错误处理

- 缺少 `wps_sid`：请先设置环境变量后重试
- 文件不存在：检查文件路径是否正确
- **400008018**（文档内容抽取失败）：该文件类型不支持 Markdown 抽取（如演示文档 .pptx）
- **400000004**（该功能暂不支持）：格式不匹配，不能用 .md 文件直接覆盖 .otl 文件
- 不支持的文件类型：写入（write）仅支持智能文档（.otl）、文字文档（.docx）、PDF 文档（.pdf）
- 401/403：凭证无效或权限不足
