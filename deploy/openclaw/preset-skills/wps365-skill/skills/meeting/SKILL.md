---
name: wps365-meeting
description: 创建/查询/取消在线会议，管理参会人（邀请/移除/列表），并支持查询会议室层级列表与某日程的会议室列表。
---

# 会议（V7）

## 何时使用

- 创建一个预约线上会议（带入会码与入会链接）
- 查询会议详情（主题、时间、入会信息）
- 取消会议
- 管理参会人：查看/邀请/移除
- **查询会议室**：管理员查会议室层级列表，或查某日程已关联的会议室列表

## 前置条件

- 已设置环境变量 `wps_sid`（或 `WPS_SID`）
- 在 `wps365-skill` 根目录执行命令

## 能力概览

| 操作 | 说明 | 常用参数 | 返回字段 |
|------|------|----------|----------|
| 创建会议 | 创建预约会议（默认非周期） | `subject`、`start_time`、`end_time`、`participant_ids`（可选） | `meeting_id`、`subject`、`start_time`、`end_time`、`join_url`（入会链接）、`meeting_code`（入会码）、`meeting_number`（会议号） |
| 查询会议 | 根据 `meeting_id` 查询详情 | `meeting_id` | 同上，含 `status`、`organizer`、`participants[]` |
| 列表会议 | 按时间范围查询会议列表 | `start_time`、`end_time` | `items[]`：含 `meeting_id`、`subject`、`start_time`、`end_time` |
| 修改会议 | 修改主题；可选修改时间 | `meeting_id`、`subject`（可选）、`start_time/end_time`（可选，需成对） | 更新后的会议对象 |
| 取消会议 | 取消会议 | `meeting_id` | 空对象 `{}` |
| 参会人列表 | 获取邀请参会人列表 | `meeting_id` | `items[]`：含 `user_id`、`name`、`email`、`status` |
| 邀请参会人 | 添加邀请参会人 | `meeting_id`、`ids` | 更新后的参会人列表 |
| 移除参会人 | 删除邀请参会人 | `meeting_id`、`ids` | 更新后的参会人列表 |
| **会议室层级列表** | 管理员-查询会议室层级列表（树形目录） | `room_level_id`（可选）、`page_size`、`page_token` | `items[]`：`id`、`name`、`parent_id`、`path`、`name_path`、`has_child` |
| **某日程会议室列表** | 获取某条日程已关联的会议室及预约结果 | `calendar_id`、`event_id` | `items[]`：`room_id`、`name`、`result`（success/failed/approving）、`fail_reason` |

> **会议ID获取**：创建会议后，从返回的 `meeting_id` 字段获取；列表查询时从 `items[].meeting_id` 获取。

> **入会信息**：
> - `join_url`：入会链接，用户点击可直接入会
> - `meeting_code`：入会码（6-11位数字）
> - `meeting_number`：会议号（与meeting_code类似，部分场景使用）

时间统一使用 **UTC ISO 8601**（如 `2026-03-02T08:00:00Z`）或 **东 8 区 ISO 8601**（如 `2026-03-02T16:00:00+08:00`）。客户端会转换为时间戳下发给 API。**时区要求**：必须带时区后缀（`Z` 或 `+08:00`），禁止使用无后缀的 `2026-03-02T14:00:00`，否则会被当作 UTC，东 8 区用户会看到会议晚 8 小时，导致 LLM 创建/修改的会议时间错误。

## 使用方式

在 `wps365-skill` 根目录执行：
```bash
python skills/meeting/run.py <子命令> [参数...]
```

## run.py 子命令

- `create` — 创建预约会议（默认非周期）
- `get <meeting_id>` — 查看会议详情
- `list --start "开始ISO" --end "结束ISO"` — 按时间范围列出会议
- `update <meeting_id> [--subject "主题"] [--start "开始ISO" --end "结束ISO"]` — 修改会议
- `cancel <meeting_id>` — 取消会议
- `list-participants <meeting_id>` — 参会人列表
- `add-participants <meeting_id> --ids "id1,id2"` — 邀请参会人
- `remove-participants <meeting_id> --ids "id1,id2"` — 移除参会人
- **会议室**
  - `list-room-levels [--room-level-id ID] [--page-size N] [--page-token TOKEN]` — 管理员-会议室层级列表（传空 `room_level_id` 表示从根下开始）
  - `list-event-rooms <calendar_id> <event_id>` — 某日程的会议室列表（`calendar_id` 可用 `primary` 表示主日历）

## 输出格式

先输出 Markdown 摘要，再输出完整 JSON（`## 原始数据 (JSON)`），便于 LLM 读取与二次处理。

## 错误处理

- 缺少 `wps_sid`：请先设置环境变量后重试
- `start_time/end_time` 非法：需为带时区的 ISO 8601（`Z` 或 `+08:00`）且 `end_time > start_time`；无时区会按 UTC 解析，东 8 区易错 8 小时
- 401/403：凭证无效或权限不足

