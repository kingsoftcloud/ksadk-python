---
name: wps365-calendar
description: 日历与日程的增删改查，调用 V7 日历接口。在需要列出/创建/修改/删除日历或日程、查询某段时间内的日程时使用。
---

# 日历与日程（V7）

日历介绍
日历 API 基于 WPS 日历功能开放了对日历、日程、参与者、会议室、忙闲等资源的操作与查询能力。你能以应用或用户的身份调用日历 API 来实现多种功能。例如：

创建日历、设置日历权限
在日历下创建、更新日程
查询用户忙闲、邀请用户、预定会议室
同步用户的请假状态
资源关系说明：

日历：管理日程的容器。每个用户或应用身份都默认拥有一个主日历，也都可以创建多个自定义日历（例如，用于"工作"、"个人"或特定项目）。
忙闲：用户在特定时间段的日程占用状态。查询忙闲接口可以批量获取多个用户的空闲时间，便于安排会议和日程。
日程：日历中的具体事项，包含时间、标题、描述等信息。日程必须属于某一个日历。
日程参与者：被邀请参加日程的用户。可以是企业内部用户或通过邮箱地址邀请的外部参与者。
日程会议室：可被日程预订的物理空间资源。通过将会议室添加到日程中，实现会议场地的预订和管理。
日历权限：定义了其他用户对特定日历的访问权限级别（游客权限、订阅者权限、编辑者权限、所有者权限），是实现日历共享的基础。

### 忙闲查询

| 操作 | 说明 | 常用参数 |
|------|------|----------|
| 查询忙闲 | 获取指定用户在时间区间的空闲/忙碌状态 | `user_ids`（必填，最多25人）、`start_time`、`end_time`（ISO 8601，区间≤31天） |

返回字段说明：
- `status`: `busy`（忙碌）、`free`（空闲）、`tentative`（暂定）、`out_of_office`（外出）
- `start_time`/`end_time`: 忙碌时间段的开始和结束时间

### 参与者管理（单独操作）

| 操作 | 说明 | 常用参数 |
|------|------|----------|
| 添加参与者 | 为日程添加参与者 | `calendar_id`、`event_id`、`attendee_user_ids` |
| 移除参与者 | 从日程移除参与者 | `calendar_id`、`event_id`、`attendee_user_ids` |
| 参与者列表 | 获取日程的参与者列表 | `calendar_id`、`event_id` |

### 会议室管理

| 操作 | 说明 | 常用参数 |
|------|------|----------|
| 列表会议室 | 查询可用的会议室资源 | `start_time`、`end_time`、`room_ids`（可选） |
| 添加会议室 | 为日程预订会议室 | `calendar_id`、`event_id`、`room_id` |
| 移除会议室 | 取消日程的会议室预订 | `calendar_id`、`event_id`、`room_id` |
用户或应用拥有并管理自己的日历。日历是日程的容器。日程可以邀请日程参与者并预订日程会议室。用户之间通过设置日历权限共享日历。

## 何时使用

- 查看我的日历列表、某个日历详情
- 创建/修改/删除日历
- 查看某段时间内的日程、某个日程详情
- 创建/修改/删除日程

## 前置条件

- 已设置环境变量 `wps_sid`（或 `WPS_SID`）
- 在 `wps365-skill` 根目录执行命令

## 日历

| 操作 | 说明 | 常用参数 | 返回字段 |
|------|------|----------|----------|
| 列表 | 获取日历列表 | `page_token`、`page_size`（可选） | `id`（calendar_id）、`summary`、`color`、`description`、`primary`、`accessRole` |
| 查看 | 根据日历 id 查详情 | `calendar_id` | 同上，含 `creationTime`、`lastModifiedTime` |
| 创建 | 新建日历 | `summary`（标题）、`color`（如 #FF0000FF）、`description`（可选） | 新建日历的完整对象，含 `id` |
| 修改 | 更新日历 | `calendar_id`，以及要改的 `summary`/`color`/`description` | 更新后的日历对象 |
| 删除 | 删除日历 | `calendar_id` | 空对象 `{}` |

主日历的 `calendar_id` 可使用 **primary**。

## 日程

| 操作 | 说明 | 常用参数 | 返回字段 |
|------|------|----------|----------|
| 列表 | 某日历下、某时间区间内的日程 | `calendar_id`、`start_time`、`end_time`（ISO 8601，区间≤31天） | `items[]`：含 `id`（event_id）、`summary`、`description`、`location`、`start.dateTime`、`end.dateTime`、`attendees[]`、`attachments[]` |
| 查看 | 根据日程 id 查详情 | `calendar_id`、`event_id` | 同上，含 `created`、`updated`、`status`、`organizer`、`reminders` 等 |
| 创建 | 新建日程 | `calendar_id`、`start_time`、`end_time`（必填），`summary`、`description`、**地点**（最多 1 个）、**附件**（file_id 列表，最多 20 个）、**参与者**（user_id 列表）均为可选 | 新建日程的完整对象，含 `id`（event_id） |
| 修改 | 更新日程 | `calendar_id`、`event_id`，以及要改的字段（含地点、附件） | 更新后的日程对象 |
| 删除 | 删除日程 | `calendar_id`、`event_id` | 空对象 `{}` |

时间统一使用 **UTC ISO 8601**（如 `2026-03-02T08:00:00Z`）或 **带时区的 ISO 8601**（如 `2026-03-02T16:00:00+08:00`），或仅日期 `yyyy-mm-dd`。**时区要求**：凡带时间的必须带时区后缀（`Z` 或 `+08:00`），禁止使用无后缀的 `2026-03-02T14:00:00`，否则东 8 区会出现约 8 小时偏差，导致 LLM 创建的日程时间错误。

- **地点**：创建/修改时可传 1 个地点名称（`location` 或 `locations`）。
- **附件**：传附件对应的 `file_id`（v7_file.id），创建最多 20 个，修改时可覆盖。
- **参与者**：仅创建时支持，传通讯录用户 id（`attendee_user_ids` 或命令行 `--attendees "id1,id2"`），创建成功后自动调用添加参与者接口。

## 使用方式

在 `wps365-skill` 根目录执行：
```bash
python skills/calendar/run.py <子命令> [参数...]
```
子命令见下方「run.py 子命令」。

## run.py 子命令

- `list-calendars` — 日历列表
- `get-calendar <calendar_id>` — 查看日历
- `create-calendar --title "标题" --color "#FF0000FF" [--desc "描述"]` — 创建日历
- `update-calendar <calendar_id> [--title 标题] [--color 颜色] [--desc 描述]` — 修改日历
- `delete-calendar <calendar_id>` — 删除日历
- `list-events <calendar_id> --start "开始时间ISO" --end "结束时间ISO"` — 日程列表
- `get-event <calendar_id> <event_id>` — 查看日程
- `create-event <calendar_id> --start "开始ISO" --end "结束ISO" [--title "标题"] [--desc "描述"] [--location "地点"] [--attach file_id ...] [--attendees "user_id1,user_id2"]` — 创建日程（支持地点、附件、参与者）
- `update-event <calendar_id> <event_id> [--title 标题] [--desc 描述] [--start ISO] [--end ISO] [--location "地点"] [--attach file_id ...]` — 修改日程
- `delete-event <calendar_id> <event_id>` — 删除日程
- `free-busy --user-ids "user_id1,user_id2" --start "开始ISO" --end "结束ISO"` — 查询忙闲（start/end 须带 Z 或 +08:00）

## 输出格式

先 Markdown 摘要，再完整 JSON；时间字段已为 UTC ISO 8601，便于 LLM 理解。

## 错误处理

- 缺少 `wps_sid`：设置环境变量后重试
- 401/403：凭证无效或过期
- 时间区间超过 31 天：缩小 start_time 与 end_time 范围
- 创建日程时 start_time/end_time 需为合法 ISO 8601 或 yyyy-mm-dd
