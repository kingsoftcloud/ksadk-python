---
name: agentengine-hermes-lifecycle
description: "Use when operating Hermes through ksadk / agentengine. Covers install, init, deploy, status, native TUI, permanent `/chat` and `/` dashboard links, connect, pairing, and batch creation of Hermes agents with a plan-first workflow. High-signal phrases include '创建 Hermes', '永久访问链接', '管理 UI', 'invoke', 'connect', and 'pairing'."
---

# AgentEngine Hermes Lifecycle

This skill is the execution guide for Hermes lifecycle operations through `agentengine`.

## Prerequisites

Before deploying Hermes, ensure credentials are configured:

```bash
# Verify configuration
agentengine config show

# Configure if missing
agentengine config wizard
```

Required for Hermes:

| Variable | Purpose |
|----------|---------|
| `KSYUN_ACCESS_KEY` | 金山云 AK |
| `KSYUN_SECRET_KEY` | 金山云 SK |
| `KSYUN_ACCOUNT_ID` | 金山云账号 ID |
| `KSYUN_REGION` | 默认 `pre-online` |
| `OPENAI_API_KEY` | 模型服务 API Key |

## When To Use

Use this skill when the user asks to:

- install or upgrade `ksadk` for Hermes
- create a Hermes project with `init -f hermes`
- deploy Hermes
- check Hermes status
- generate permanent chat and management URLs
- use `agentengine invoke` for Hermes
- run `agentengine hermes connect`
- run `agentengine hermes pairing`
- create multiple Hermes agents and summarize the links

## Example Requests

These example requests should strongly trigger this Hermes skill:

- 帮我初始化一个 Hermes 项目，然后部署到 `pre-online`
- 帮我创建一个 Hermes agent，并返回永久 `/chat` 和 `/` 链接
- 创建 3 个 Hermes agent，给我永久访问 URL 和管理 UI URL
- 帮我看看这个 Hermes 实例是不是 `RUNNING`
- 用 `agentengine invoke` 连一下 Hermes TUI
- 帮我跑一遍 `agentengine hermes connect`
- 帮我查一下 Hermes 的 pairing 列表
- 用 CLI 给当前 Hermes 生成永久不过期的 chat 链接和管理链接
- Deploy Hermes and return permanent `/chat` and `/` URLs
- Run Hermes deploy, status, connect, and pairing for me

## Hard Rules

- Default to a plan-first reply. Show the commands, target region, directories, naming scheme, and whether permanent links will be created.
- Default Hermes region is `pre-online`.
- If the user does not specify a working directory, create the project under `/tmp/<agent-name>`.
- Before deploy, ensure `.env` contains usable `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL_NAME`.
- If `OPENAI_BASE_URL` and `OPENAI_MODEL_NAME` are missing, fill:
  - `OPENAI_BASE_URL=http://kspmas.ksyun.com/v1`
  - `OPENAI_MODEL_NAME=glm-5.1`
- Default Hermes control plane is `AGENTENGINE_SERVER_URL=http://aicp.inner.api.ksyun.com`.
- Permanent links must always be created twice:
  - `--path /chat`
  - `--path /`
- After link creation, validate with `agentengine dashboard share list` and confirm:
  - one link has `path=/chat`
  - one link has `path=/`
  - both report `expires_at=永久`
- Batch mode covers create, deploy, status, and permanent links only. `connect` and `pairing` remain single-instance operations.

## Workflow

Read [references/hermes-lifecycle.md](references/hermes-lifecycle.md) for the main command sequence.

- For permanent links, read [references/dashboard-links.md](references/dashboard-links.md).
- For failures or unexpected responses, read [references/troubleshooting.md](references/troubleshooting.md).

## Output Contract

Single-instance final output should include:

- project directory
- agent name
- agent id
- status
- permanent chat URL
- permanent management URL

Batch final output should include one row per agent with:

- `name`
- `agent_id`
- `status`
- `chat_url`
- `manage_url`
