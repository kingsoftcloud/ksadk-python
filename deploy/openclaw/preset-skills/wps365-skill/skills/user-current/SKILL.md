---
name: wps365-user-current
description: 查询当前登录用户信息，调用 V7 接口 GET /v7/users/current。在需要获取当前用户身份、昵称、企业、部门等信息时使用。
---

# 查询当前用户信息（V7）

## 何时使用

- 用户或对话需要「当前登录用户」「我是谁」「当前账号」等信息时
- 需要当前用户的 id、昵称、企业、部门、邮箱、手机等基础信息时

## 前置条件

- 已设置环境变量 `wps_sid`（或 `WPS_SID`）
- 在 `wps365-skill` 根目录执行命令

## 步骤

在 `wps365-skill` 根目录执行：
```bash
python skills/user-current/run.py
```
输出为 Markdown 摘要 + 完整 JSON。

## 输出格式

先给一段简短 Markdown 摘要，再附完整 JSON（可与接口原样一致或精简字段）。

**Markdown 示例：**

```markdown
## 当前用户

- **用户**：{user_name}
- **用户ID**：{id}
- **企业ID**：{company_id}
- **部门**：{depts 简要列表或「无」}
```

**JSON 说明：** 返回中的时间字段（如 `ctime`、`mtime`）已统一为 **UTC ISO 8601** 字符串（如 `2026-03-02T08:00:00Z`），便于 LLM 理解。

```json
{
  "id": "...",
  "user_name": "...",
  "company_id": "...",
  "avatar": "...",
  "email": "...",
  "phone": "...",
  "ctime": "2026-03-02T08:00:00Z",
  "mtime": "2026-03-02T08:00:00Z",
  "depts": [...]
}
```

## 错误处理

- 若提示缺少 `wps_sid`：请先设置环境变量后再调用
- 若返回 401/403：凭证无效或已过期，需重新获取 wps_sid
