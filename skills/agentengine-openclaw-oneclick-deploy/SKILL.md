---
name: agentengine-openclaw-oneclick-deploy
description: "Use when operating OpenClaw through ksadk / agentengine. Covers install, deploy, status and list, permanent dashboard links, batch creation, and single-instance channel connect flows with a plan-first workflow. High-signal phrases include '部署 OpenClaw', 'dashboard 链接', '不过期链接', 'channel connect', '微信', and '飞书'."
---

# AgentEngine OpenClaw Lifecycle

This skill owns OpenClaw lifecycle operations through `agentengine`. It keeps the existing skill name, but expands the scope from one-click deploy to the broader OpenClaw CLI lifecycle.

## Prerequisites

Before deploying OpenClaw, ensure credentials are configured:

```bash
# Verify configuration
agentengine config show

# Configure if missing
agentengine config wizard
```

Required for OpenClaw:

| Variable | Purpose |
|----------|---------|
| `KSYUN_ACCESS_KEY` | 金山云 AK |
| `KSYUN_SECRET_KEY` | 金山云 SK |
| `KSYUN_ACCOUNT_ID` | 金山云账号 ID |
| `KSYUN_REGION` | 默认 `cn-beijing-6` |
| `OPENAI_API_KEY` | 模型服务 API Key (运行时需要) |

## When To Use

Use this skill when the user asks to:

- install or upgrade `ksadk` for OpenClaw
- deploy OpenClaw
- list or inspect OpenClaw instances
- generate permanent dashboard links
- create multiple OpenClaw instances
- run `agentengine openclaw channel connect`

## Example Requests

These example requests should strongly trigger this OpenClaw skill:

- 帮我部署一个 OpenClaw 实例
- 用 `agentengine` 给 OpenClaw 生成永久 dashboard 链接
- 帮我批量创建 2 个 OpenClaw，并汇总访问 URL
- 帮我看一下这个 OpenClaw 的状态
- 跑一下 `agentengine openclaw channel connect`
- 帮我把 OpenClaw 接到微信
- 帮我把 OpenClaw 接到飞书
- 给这个 OpenClaw 创建一个永久不过期的访问地址
- Deploy OpenClaw in `cn-beijing-6` and return the dashboard URL
- Run OpenClaw deploy and channel connect for me

## Hard Rules

- Default to a plan-first reply. Show the commands, target region, resources, naming strategy, and whether permanent dashboard links will be created.
- Default OpenClaw region is `cn-beijing-6`.
- OpenClaw does not require `init`; deploy can run directly from a temporary working directory.
- Prefer current canonical commands such as `agentengine openclaw deploy`, `agentengine openclaw status`, `agentengine dashboard open`.
- Batch mode covers deploy, status, and permanent dashboard links only.
- `channel connect` is single-instance only and should not be auto-run in batch flows.
- Use `--output json` when parsing deploy results, dashboard links, or share-list validation.
- After creating a permanent dashboard link, validate it with `agentengine dashboard share list`.

## Workflow

Read [references/openclaw-lifecycle.md](references/openclaw-lifecycle.md) for deploy and batch behavior.

- For permanent links, read [references/dashboard-links.md](references/dashboard-links.md).
- For IM onboarding, read [references/channel-connect.md](references/channel-connect.md).
- For failures or environment issues, read [references/troubleshooting.md](references/troubleshooting.md).

## Output Contract

Single-instance final output should include:

- instance name
- agent id
- status
- permanent dashboard URL

Batch final output should include one row per instance with:

- `name`
- `agent_id`
- `status`
- `dashboard_url`
