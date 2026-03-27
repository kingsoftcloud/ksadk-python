---
name: wps365-contacts
description: 通讯录按人名搜索用户信息，调用 V7 企业用户搜索接口。根据姓名查询用户，若存在同名则返回多条，便于 LLM 或用户进一步选择。
---

# 通讯录·按人名查用户（V7）

## 何时使用

- 需要根据**姓名**查找企业内用户
- 不确定唯一 id，只有名字时
- 存在同名用户时需返回**全部匹配结果**供选择

## 前置条件

- 已设置环境变量 `wps_sid`（或 `WPS_SID`）
- 在 `wps365-skill` 根目录执行命令

## 接口说明

| 操作 | 说明 | 参数 | 返回字段 |
|------|------|------|----------|
| 搜索 | 按关键字（人名）搜索用户，支持模糊匹配 | `keyword`（必填），可选 `page_size`、`page_token` | `items[]`：含 `user_id`、`name`、`department`、`email`、`phone`、`position`、`avatar_url`、`status` |

### 返回字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | string | 用户唯一标识，用于其他API（如添加日程参与者、会议邀请） |
| `name` | string | 用户姓名 |
| `department` | string | 所属部门 |
| `email` | string | 邮箱地址 |
| `phone` | string | 手机号 |
| `position` | string | 职位 |
| `avatar_url` | string | 头像URL |
| `status` | string | 用户状态（如 `active`） |

> **用户ID用途**：获取的 `user_id` 可用于：
> - 日程创建时添加参与者（`attendee_user_ids`）
> - 会议创建时邀请参会人（`participant_ids`）
> - 其他需要指定用户的场景

> **搜索规则**：支持模糊匹配（按姓名），返回所有匹配的用户。若存在同名用户，会返回全部结果供选择。

## 使用方式

在 `wps365-skill` 根目录执行：
```bash
python skills/contacts/run.py <子命令> [参数...]
```
子命令见下方「run.py 子命令」。

## run.py 子命令

- `search <人名>` 或 `search --keyword "人名"` — 按人名搜索，输出 Markdown 摘要 + 完整 JSON；若有多条同名会全部列出。

## 输出格式

- 先 Markdown：根据「xxx」找到 N 个用户；若无则「未找到匹配用户」；若有则逐条列出姓名、用户ID、部门、邮箱等。
- 再 JSON：完整 `data`（含 `items`、`next_page_token` 等），便于 LLM 解析。

## 错误处理

- 缺少 `wps_sid`：设置环境变量后重试
- 401/403：凭证无效或过期
