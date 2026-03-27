---
name: wps365-im
description: 聊天会话管理，获取会话列表、最近会话、历史消息，发送文本/Markdown/富文本/云文档消息等。
---

# 聊天会话·IM（V7）

## 何时使用

- 需要获取当前用户的会话列表
- 需要获取最近会话列表（带未读数）
- 需要根据关键字搜索会话
- 需要**全局搜索消息**（跨会话搜索关键字）
- 需要查看指定会话的详细信息
- 需要获取会话的历史消息记录
- 需要向指定会话发送消息（文本、Markdown、富文本、云文档等）
- 需要撤回已发送的消息

## 前置条件

- 已设置环境变量 `wps_sid`（或 `WPS_SID`）
- 在 `wps365-skill` 根目录执行命令

## 能力概览

| 操作 | 说明 | 常用参数 |
|------|------|----------|
| list | 获取会话列表 | `--page-size` |
| recent | 获取最近会话（含未读） | `--filter-unread`、`--filter-mention-me` |
| search | 按名称搜索会话 | 关键字、`--page-size` |
| search-messages | 全局搜索消息 | `--keyword`、`--chat-ids`、`--start-time` |
| get | 获取会话详情 | `chat_id` |
| history | 获取历史消息 | `chat_id`、`--start-time`、`--end-time` |
| send | 发送消息（默认 text+Markdown） | `chat_id`、内容、`--plain`、`--type` |
| recall | 撤回消息 | `chat_id`、`message_id` |

## 使用方式

在 `wps365-skill` 根目录执行：
```bash
python skills/im/run.py <子命令> [参数...]
```

## 命令行示例

```bash
# 获取会话列表
python skills/im/run.py list
python skills/im/run.py list --page-size 20

# 获取最近会话
python skills/im/run.py recent
python skills/im/run.py recent --filter-unread
python skills/im/run.py recent --filter-mention-me

# 搜索会话（按名称）
python skills/im/run.py search "关键字"

# 全局搜索消息（跨所有会话搜索关键字）
python skills/im/run.py search-messages --keyword "关键字"
python skills/im/run.py search-messages --keyword "关键字" --start-time 2026-01-01T00:00:00Z

# 获取会话详情
python skills/im/run.py get <chat_id>

# 获取历史消息
python skills/im/run.py history <chat_id>
python skills/im/run.py history <chat_id> --start-time 2024-01-01T00:00:00Z

# 发送消息（默认以 text 类型指定 markdown 发送，支持 **加粗**、列表等）
python skills/im/run.py send <chat_id> "**你好**，这是 Markdown 内容"
# 以纯文本发送（不指定 markdown）
python skills/im/run.py send <chat_id> "纯文本" --plain
# @某人 或 @所有人：以 text 类型发送（规范示例 id="0"），正文用闭合标签 <at id="1">展示名</at>（会转为 0-based）
python skills/im/run.py send <chat_id> "请 <at id=\"1\">杨彬</at> 查收" --mention 1388382966
python skills/im/run.py send <chat_id> "通知：<at id=\"1\">所有人</at> 请查收" --mention all
python skills/im/run.py send <chat_id> --type rich_text --rich-text '<json>'
python skills/im/run.py send <chat_id> --type file --file '<json>'

# 撤回消息
python skills/im/run.py recall <chat_id> <message_id>
```

## 子命令

### list - 获取会话列表

获取当前用户加入的所有会话（群聊+单聊）。

```bash
python skills/im/run.py list [--page-size N]
```

### recent - 获取最近会话

获取最近会话列表，包含未读数、置顶、免打扰等状态。

```bash
python skills/im/run.py recent [--page-size N] [--filter-unread] [--filter-mention-me]
```

### search - 搜索会话

根据会话名称搜索会话。

```bash
python skills/im/run.py search "关键字" [--page-size N]
```

### search-messages - 全局搜索消息

在所有会话中搜索包含关键字的消息。支持多种搜索条件。

```bash
python skills/im/run.py search-messages --keyword "关键字"
python skills/im/run.py search-messages --keyword "关键字" --page-size 50
python skills/im/run.py search-messages --start-time 2026-01-01T00:00:00Z --end-time 2026-03-01T00:00:00Z
python skills/im/run.py search-messages --chat-ids "chat_id1,chat_id2"
python skills/im/run.py search-messages --sender-ids "user_id1,user_id2"
python skills/im/run.py search-messages --msg-types "text,file,image,cloud_file"
```

**参数说明：**
- `--keyword/-k`: 搜索关键字
- `--chat-ids/-c`: 指定会话ID列表，逗号分隔
- `--sender-ids/-s`: 指定发送者ID列表，逗号分隔
- `--msg-types/-t`: 消息类型过滤（text, file, image, cloud_file, audio, video, location, link）
- `--page-size/-p`: 分页大小，默认20
- `--start-time`: 起始时间（UTC ISO 8601）
- `--end-time`: 结束时间（UTC ISO 8601）
- `--order/-o`: 排序方式（by_create_time_desc 或 by_create_time_asc）

### get - 获取会话详情

```bash
python skills/im/run.py get <chat_id>
```

### history - 获取历史消息

```bash
python skills/im/run.py history <chat_id> [--page-size N] [--start-time TIME] [--end-time TIME] [--order ORDER]
```

- 时间参数使用 UTC ISO 8601 格式，如 `2024-01-01T00:00:00Z`
- `order`: `by_create_time_desc`（默认，降序）或 `by_create_time_asc`（升序）

### send - 发送消息

```bash
python skills/im/run.py send <chat_id> [text] [--type TYPE] [--plain] [options]
```

**默认行为**：不指定 `--type` 时，以 **text 类型 + markdown** 发送（请求体 `text: { type: "markdown", content }`），支持 **加粗**、列表、引用等。加 `--plain` 时改为纯文本（不指定 markdown）。

**@（at）标签**：通过 `--mention` / `-M` 传入被 @ 对象（顺序与正文 at 一一对应），可多次使用。
- **默认**：有 @ 时以 **text + plain** 发送，发送前转为 **0-based**，保证 @ 展示。
- **at + Markdown**：加 `--at-markdown` 时以 **rich_text** 发送（文本段为 markdown、@ 为 mention 元素），可同时展示 @ 与加粗/列表等。
- **正文格式**：须为**闭合标签**，标签内为**展示名**，例如 `你好<at id="1">杨彬</at>`。
- **id**：命令行第一个 @ 写 `<at id="1">`，第二个写 `<at id="2">`；默认发送时转为 0-based，`--at-markdown` 时用 rich_text 不转换。
- @某人：`--mention <user_id>`；@所有人：`--mention all`。
- 示例：`send <chat_id> "请 <at id=\"1\">杨彬</at> 查收" --mention 1388382966`；at+Markdown：`... --mention 1388382966 --at-markdown`

**消息类型：**

| 类型 | 说明 | 示例 |
|------|------|------|
| text | 文本消息（默认带 markdown） | `send 123 "**你好**"`、`send 123 "纯文本" --plain` |
| rich_text | 富文本消息 | `send 123 --type rich_text --rich-text '<json>'` |
| file | 文件/云文档 | `send 123 --type file --file '<json>'` |
| image | 图片消息 | `send 123 --type image --image-key '<json>'` |
| card | 卡片消息 | `send 123 --type card --card '<json>'` |

**发送 Markdown 示例（默认）：**
```bash
python skills/im/run.py send 123456 "## 会议提醒\n\n**时间**：周二 15:00\n\n请准时参加。"
```

**发送富文本示例：**
```bash
python skills/im/run.py send 123456 --type rich_text --rich-text '{"elements":[{"type":"text","index":1,"indent":0,"alt_text":"Hello","style_content":{"text":"Hello World","style":{"bold":true}}}]}'
```

**发送云文档示例：**
> **重要**: `id` 字段需要使用 `link_id` 的值，而不是 `file_id`

```bash
python skills/im/run.py send 123456 --type file --file '{"type":"cloud","cloud":{"id":"xxx","link_url":"https://www.kdocs.cn/l/xxx","link_id":"xxx"}}'
```

> 注意：上传云文档后，返回的 JSON 中 `link_id` 即为发送消息时 `id` 字段所需的值。

### recall - 撤回消息

```bash
python skills/im/run.py recall <chat_id> <message_id>
```

## 输出格式

先输出 Markdown 摘要，再输出完整 JSON（`## 原始数据 (JSON)`）。

## 错误处理

- 缺少 `wps_sid`：设置环境变量后重试
- 401/403：凭证无效或过期
- 会话/消息不存在：检查 ID 是否正确
