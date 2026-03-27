---
name: wps365-skills
description: WPS 365 V7 API SKILL 工具集，集成通讯录、日历、会议、云文档、多维表、IM 等能力。
---

# WPS 365 SKILL 工具集

基于 WPS 365 V7 API 封装的命令行工具，帮助你快速完成企业协作任务。

## 快速开始

```bash
# 设置认证（只需设置一次）
export WPS_SID="你的WPS_SID值"

# 在 wps365-skills 根目录执行
cd wps365-skills
```

## SKILL 概览

| SKILL | 功能 | 典型场景 |
|-------|------|----------|
| [contacts](#contacts) | 通讯录搜索 | 查人、获取用户ID |
| [user-current](#user-current) | 当前用户 | 确认当前登录身份 |
| [calendar](#calendar) | 日历与日程 | 创建/查询日程、忙闲查询 |
| [meeting](#meeting) | 会议管理 | 创建会议、管理参会人 |
| [drive](#drive) | 云文档 | 创建/上传/更新/列表/详情/下载、**读取/抽取正文**（read/extract）、**Markdown 写入**（write）、搜索（search）/最近（latest）、link-meta、收藏与标签管理、回收站管理、文件操作（新建/复制/移动/重命名/另存为/重名检查）、分享开关 |
| [dbsheet](#dbsheet) | 多维表 | 新建多维表（配合 Drive create）、Schema、列举/检索/增删改记录、创建视图 |
| [im](#im) | 聊天消息 | 会话管理、发送消息、搜索 |

---

## contacts

按姓名搜索企业通讯录，获取用户 ID。

```bash
# 搜索用户
python skills/contacts/run.py search "姓名"

# 获取用户 ID 后可用于添加日程/会议参与者
```

**用途**：获取的 `user_id` 可用于日程/会议的参与者邀请。

---

## user-current

查询当前登录用户信息。

```bash
python skills/user-current/run.py
```

返回：用户ID、昵称、企业、部门、邮箱、手机等。

---

## calendar

日历与日程管理。

```bash
# 列出日历
python skills/calendar/run.py list-calendars

# 创建日程
python skills/calendar/run.py create-event --calendar-id <id> --title "会议" --start 2026-03-04T03:00:00Z --end 2026-03-04T04:00:00Z

# 查询忙闲（用户/会议室在指定区间的忙时段，其余为空闲）
python skills/calendar/run.py free-busy --user-ids "user_id1,user_id2" --start 2026-03-04T00:00:00Z --end 2026-03-05T00:00:00Z

# 更多命令：get-event, update-event, delete-event, list-events
```

**时间格式**：须带时区，如 `2026-03-04T03:00:00Z` 或 `2026-03-04T11:00:00+08:00`（禁止无后缀，防东 8 区错 8 小时）。

---

## dbsheet

多维表（DbSheet）查询与管理。需应用开通 `kso.internal.dbsheet.read` 或 `readwrite`。

**新建多维表**：先用 Drive `create <文件名.dbt>` 在云盘创建 .dbt 文件得到 file_id，再用本技能 create-sheet 建表、create-records 写数据。

```bash
# 获取多维表结构（数据表、视图、字段）
python skills/dbsheet/run.py schema <file_id>

# 列举记录（file_id、sheet_id 来自 Schema）
python skills/dbsheet/run.py list-records <file_id> <sheet_id> --page-size 20

# 检索单条/多条记录
python skills/dbsheet/run.py get-record <file_id> <sheet_id> <record_id>
python skills/dbsheet/run.py search-records <file_id> <sheet_id> <record_id1> <record_id2>

# 创建数据表（需先有 file_id，见「新建多维表」）
python skills/dbsheet/run.py create-sheet <file_id> --json '{"name":"表名","fields":[...],"views":[{"name":"表格视图","view_type":"Grid"}]}'

# 批量创建/更新/删除记录（--json 传 records 数组）
python skills/dbsheet/run.py create-records <file_id> <sheet_id> --json '[{"标题":"值"}]'
python skills/dbsheet/run.py update-records <file_id> <sheet_id> --json '[{"id":"recXXX","fields_value":"{\"标题\":\"新值\"}"}]'
python skills/dbsheet/run.py delete-records <file_id> <sheet_id> <record_id1> <record_id2>

# 新建 .dbt 后若希望新记录在表前列：先删空记录再插入
python skills/dbsheet/run.py delete-empty-records <file_id> <sheet_id>

# 创建视图
python3 skills/dbsheet/run.py create-view <file_id> <sheet_id> --name <view_name> --type <view_type> --options '<options_json>'
```

**file_id**：多维表云文档文件 ID（新建时由 Drive `create 文件名.dbt` 返回）；**sheet_id**：数据表 ID（从 schema 的 `sheets[].id` 或 create-sheet 返回获取）。

---

## meeting

在线会议管理。

```bash
# 创建会议
python skills/meeting/run.py create --subject "主题" --start 2026-03-04T03:00:00Z --end 2026-03-04T04:00:00Z --participants "user_id1,user_id2"

# 查询会议
python skills/meeting/run.py get <meeting_id>

# 会议列表
python skills/meeting/run.py list --start 2026-03-01T00:00:00Z --end 2026-03-07T00:00:00Z

# 取消会议
python skills/meeting/run.py cancel <meeting_id>

# 管理参会人
python skills/meeting/run.py add-participants <meeting_id> --ids "user_id"
python skills/meeting/run.py remove-participants <meeting_id> --ids "user_id"
python skills/meeting/run.py list-participants <meeting_id>
```

**返回信息**：meeting_id、join_url（入会链接）、meeting_code（入会码）  
**时间格式**：start/end 须带 `Z` 或 `+08:00`，禁止无后缀（防东 8 区错 8 小时）。

---

## drive

金山云文档管理。支持云盘文件流转（创建/上传/更新/列表/详情/下载）、文档内容处理（read/extract 抽取、write 写入）、检索能力（search/latest）、组织管理（收藏/标签/回收站）、文件操作（新建/复制/移动/重命名/另存为/重名检查）、分享开关与 link-meta 解析。

```bash
# 在云盘下创建文件（支持 .otl、.dbt 等）
python skills/drive/run.py create 产品需求.dbt
python skills/drive/run.py create 文档.otl --path "我的文档/子目录" --drive private --on-conflict rename

# 上传文件（.md 会以智能文档形式上传）
python skills/drive/run.py upload /path/to/file.md
python skills/drive/run.py upload /path/to/file.docx
python skills/drive/run.py upload /path/to/file.md --drive private --path "我的文档/子目录"

# 更新现有文件（上传本地文件新版本覆盖）
python skills/drive/run.py update <file_id|link_id> /path/to/file.docx

# 文件列表、详情、下载
python skills/drive/run.py list
python skills/drive/run.py get <file_id>
python skills/drive/run.py download <file_id>

# 读取文档为 Markdown/正文（read 为 extract 的别名；支持 .otl、.docx、.pdf）
python skills/drive/run.py read <file_id>
python skills/drive/run.py extract <file_id> --format markdown

# 将 Markdown 写入已有文档（智能文档插入；文字/PDF 为转换后覆盖）
python skills/drive/run.py write <file_id> --content "# 标题\n\n内容"
python skills/drive/run.py write <file_id> --file /path/to/content.md
python skills/drive/run.py write <file_id> --file content.md --template template.docx

# 搜索/最近/分享链接解析（link_id → file_id）
python skills/drive/run.py search "关键词"
python skills/drive/run.py latest --page-size 20
python skills/drive/run.py link-meta <link_id>

# 文件管理（新建/复制/移动/重命名/另存为/重名检查）
python skills/drive/run.py create "项目资料" --drive <drive_id> --parent-id <parent_id> --file-type folder
python skills/drive/run.py file-copy <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <dst_parent_id>
python skills/drive/run.py file-move <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <dst_parent_id>
python skills/drive/run.py file-rename <drive_id> <file_id> --dst-name "新文件名.docx"
python skills/drive/run.py file-save-as <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <dst_parent_id> --name "副本.docx"
python skills/drive/run.py file-check-name <drive_id> <parent_id> --name "新文件名.docx"

# 收藏/标签管理
python skills/drive/run.py star --page-size 20
python skills/drive/run.py star-add-items --objects "<file_id1>,<file_id2>"
python skills/drive/run.py star-remove-items --objects "<file_id1>"
python skills/drive/run.py tags --label-type custom --page-size 20
python skills/drive/run.py tag-create --name "重点项目"
python skills/drive/run.py tag-add-objects <label_id> --objects "<file_id1>,<file_id2>"
python skills/drive/run.py tag-remove-objects <label_id> --objects "<file_id2>"

# 回收站管理
python skills/drive/run.py deleted-list --page-size 20
python skills/drive/run.py deleted-restore <file_id>

# 分享开关
python skills/drive/run.py file-open-link <drive_id> <file_id> --scope anyone
python skills/drive/run.py file-close-link <drive_id> <file_id> --mode pause

```

**读取（read/extract）**：支持智能文档（.otl）、文字文档（.docx）、PDF 文档（.pdf）；默认输出 markdown。演示文档（.pptx）暂不支持抽取。

**写入（write）**：智能文档用 insertContent 插入；文字文档/PDF 为 Markdown 转 DOCX/PDF 后覆盖。仅支持 .otl、.docx、.pdf。

---

## im

聊天会话与消息管理。

### 会话管理

```bash
# 会话列表
python skills/im/run.py list

# 最近会话（带未读数）
python skills/im/run.py recent

# 搜索会话
python skills/im/run.py search "关键字"

# 会话详情
python skills/im/run.py get <chat_id>
```

### 消息管理

```bash
# 历史消息
python skills/im/run.py history <chat_id>

# 全局搜索消息（跨所有会话）
python skills/im/run.py search-messages --keyword "关键字"

# 发送消息
python skills/im/run.py send <chat_id> "文本内容"

# 发送富文本
python skills/im/run.py send <chat_id> --type rich_text --rich-text '<json>'

# 发送云文档
python skills/im/run.py send <chat_id> --type file --file '<json>'

# 撤回消息
python skills/im/run.py recall <chat_id> <message_id>
```

**消息类型**：
- `text` - 文本消息
- `rich_text` - 富文本（支持加粗、斜体等样式）
- `file` - 文件/云文档
- `image` - 图片（需要 storage_key）

---

## 常见场景

### 场景1：创建会议并邀请参会人

```bash
# 1. 查找参会人
python skills/contacts/run.py search "张三"

# 2. 查询忙闲
python skills/calendar/run.py free-busy --user-ids "user_id" --start 2026-03-04T00:00:00Z --end 2026-03-04T23:59:59Z

# 3. 创建会议
python skills/meeting/run.py create --subject "项目评审" --start 2026-03-04T03:00:00Z --end 2026-03-04T04:00:00Z --participants "user_id1,user_id2"
```

### 场景2：发送云文档到群聊

```bash
# 1. 上传文档到云端（.md 会以智能文档上传，返回 link_id / link_url）
python skills/drive/run.py upload /path/to/doc.docx

# 2. 上传输出中会包含「发送云文档消息所需信息」JSON

# 3. 发送到群聊（id 字段填 link_id，不是 file_id）
python skills/im/run.py send <chat_id> --type file --file '{"type":"cloud","cloud":{"id":"<link_id>","link_url":"<link_url>","link_id":"<link_id>"}}'
```

### 场景3：搜索历史消息

```bash
# 全局搜索关键字
python skills/im/run.py search-messages --keyword "需求"

# 按时间范围搜索
python skills/im/run.py search-messages --start-time 2026-01-01T00:00:00Z --end-time 2026-03-01T00:00:00Z

# 按会话搜索
python skills/im/run.py search-messages --chat-ids "chat_id1,chat_id2"
```

### 场景4：新建多维表并写入数据

```bash
# 1. 在云盘创建 .dbt 文件（返回 file_id、link_id、link_url）
python skills/drive/run.py create 反馈管理.dbt

# 2. 在多维表内创建数据表（取返回的 sheet.id 作为 sheet_id）
python skills/dbsheet/run.py create-sheet <file_id> --json '{"name":"反馈","fields":[{"name":"问题","field_type":"MultiLineText"},{"name":"反馈人","field_type":"SingleLineText"}],"views":[{"name":"表格视图","view_type":"Grid"}]}'

# 3. 可选：删空记录后插入，使新记录在表前列
python skills/dbsheet/run.py delete-empty-records <file_id> <sheet_id>
python skills/dbsheet/run.py create-records <file_id> <sheet_id> --json '[{"问题":"示例","反馈人":"张三"}]'

# 4. 发送多维表到会话（cloud.id 填 link_id）
python skills/im/run.py send <chat_id> --type file --file '{"type":"cloud","cloud":{"id":"<link_id>","link_url":"<link_url>","link_id":"<link_id>"}}'
```

### 场景5：在云盘内归档文档（复制+移动+重命名）

```bash
# 1. 复制到归档目录
python skills/drive/run.py file-copy <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <archive_parent_id>

# 2. 将原文件移动到历史目录
python skills/drive/run.py file-move <src_drive_id> <file_id> --dst-drive-id <dst_drive_id> --dst-parent-id <history_parent_id>

# 3. 统一重命名
python skills/drive/run.py file-rename <dst_drive_id> <file_id> --dst-name "项目周报-2026W11.docx"
```

### 场景6：文档发布前检查（重名检查+另存为+开启分享）

```bash
# 1. 检查目标目录是否同名
python skills/drive/run.py file-check-name <drive_id> <parent_id> --name "发布版-需求说明.docx"

# 2. 另存为发布副本
python skills/drive/run.py file-save-as <drive_id> <file_id> --dst-drive-id <drive_id> --dst-parent-id <parent_id> --name "发布版-需求说明.docx"

# 3. 开启分享并设置范围
python skills/drive/run.py file-open-link <drive_id> <new_file_id> --scope anyone
```

### 场景7：资料治理（标签+收藏+回收站恢复）

```bash
# 1. 给关键文件打标签并加入收藏
python skills/drive/run.py tag-create --name "重点资料"
python skills/drive/run.py tag-add-objects <label_id> --objects "<file_id1>,<file_id2>"
python skills/drive/run.py star-add-items --objects "<file_id1>,<file_id2>"

# 2. 误删后从回收站恢复
python skills/drive/run.py deleted-restore <file_id>
```

---

## 错误处理

| 错误 | 原因 | 解决方案 |
|------|------|----------|
| 缺少 wps_sid | 未设置环境变量 | `export WPS_SID=xxx` |
| csrfCheckFailed | CSRF 验证失败 | 检查 Cookie 配置 |
| 401/403 | 凭证无效/过期 | 重新获取 wps_sid |

---

## 时间格式与时区（重要，防 LLM 出错）

所有涉及**开始/结束时间**的参数（日历、会议、忙闲等）必须使用 **带时区的 ISO 8601**，**禁止使用无时区后缀**的写法，否则在东 8 区会导致**约 8 小时偏差**（会议/日程会晚 8 小时或显示错误）。可默认使用东8区。

- **推荐（二选一）**：
  - UTC：`2026-03-04T06:00:00Z`（表示北京时间 14:00）
  - 东 8 区：`2026-03-04T14:00:00+08:00`（北京时间 14:00）
- **错误示例**：`2026-03-04T14:00:00`（无 `Z` 或无 `+08:00`）会被当作 14:00 UTC，对应北京时间 22:00，易导致会议/日程晚 8 小时。

调用日历、会议、忙闲等 SKILL 时，请始终在时间字符串末尾带 `Z`（UTC）或 `+08:00`（东 8 区）。

---

## 获取帮助

```bash
# 查看所有 SKILL
ls skills/*/SKILL.md

# 查看具体 SKILL 帮助
python skills/<skill>/run.py --help
python skills/<skill>/run.py <子命令> --help
```