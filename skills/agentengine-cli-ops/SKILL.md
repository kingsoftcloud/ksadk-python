---
name: agentengine-cli-ops
description: "Route ksadk / agentengine CLI tasks across Hermes and OpenClaw. Use when the user wants to install ksadk, deploy Hermes or OpenClaw, create one or many instances, generate permanent dashboard links, or run connect / pairing style operations with a plan-first workflow. High-signal phrases include '部署 Hermes', '部署 OpenClaw', '永久链接', '批量创建', 'connect', and 'pairing'."
---

# AgentEngine CLI Ops

This skill is the router for `ksadk` / `agentengine` CLI tasks. It should not restate every product workflow inline. Its job is to recognize the target product, present a short execution plan first, and then follow the correct child skill.

## Prerequisites

Before any deploy operation, ensure cloud credentials are configured:

```bash
# Interactive wizard (recommended for first run)
agentengine config wizard

# Or verify current config
agentengine config show
```

Required variables:

| Variable | Required For |
|----------|--------------|
| `KSYUN_ACCESS_KEY` | 所有云端部署 |
| `KSYUN_SECRET_KEY` | 所有云端部署 |
| `KSYUN_ACCOUNT_ID` | 所有云端部署 |
| `OPENAI_API_KEY` | Hermes / OpenClaw 运行时 |

Read [references/prerequisites.md](references/prerequisites.md) for full configuration details.

## When To Use

Use this skill when the user asks for any of the following:

- install or upgrade `ksadk` / `agentengine`
- deploy Hermes
- deploy OpenClaw
- create multiple Hermes or OpenClaw instances
- generate permanent dashboard links
- run `connect`, `pairing`, or related CLI operations
- turn a natural-language request into a concrete `agentengine` command flow

## Example Requests

These example requests should strongly trigger this router skill before handing off to the Hermes or OpenClaw child skill:

- 帮我用 `ksadk` 部署一个 Hermes agent
- 帮我创建 3 个 Hermes agent，并给出永久访问 URL 和管理 UI URL
- 帮我一键部署 OpenClaw，并返回 dashboard 永久链接
- 用 `agentengine` 帮我跑 `connect` 或 `pairing`
- 帮我把上面的 AgentEngine CLI 操作整理成执行计划再执行
- 帮我检查 `ksadk` 是否需要升级，然后继续部署 Hermes
- 帮我批量创建 2 个 OpenClaw 实例，并汇总访问地址
- Use `agentengine` to deploy Hermes and return permanent share URLs
- Deploy OpenClaw and give me the dashboard link
- Plan and run the CLI workflow for Hermes or OpenClaw

Do not use this skill as the deep implementation reference. Once the target product is clear, read the corresponding child skill:

- Hermes: `agentengine-hermes-lifecycle`
- OpenClaw: `agentengine-openclaw-oneclick-deploy`

## Hard Rules

- Default to a plan-first response. Summarize target product, commands to run, resources that will be created, default region, batch naming, and whether permanent links will be generated.
- If the user explicitly asks to execute immediately, skip the plan phase and run the commands.
- Prefer current canonical commands such as `agentengine hermes ...`, `agentengine openclaw ...`, `agentengine dashboard open`, `agentengine agent invoke`.
- Use `--output json` whenever the output will be parsed or reused in a later step.
- Keep Hermes and OpenClaw flows separate. Do not mix their commands into one command sequence.
- For batch creation, read `references/shared-defaults.md` before execution so naming, working directory, and output fields stay consistent.

## Routing Workflow

Read [references/routing.md](references/routing.md) at the start of any request.

- If the request mentions Hermes, hosted Hermes, Hermes TUI, pairing, or `/chat` plus Hermes, switch to the Hermes child skill.
- If the request mentions OpenClaw, channel connect, Weixin, Feishu, or OpenClaw dashboard, switch to the OpenClaw child skill.
- If the request is only about installing `ksadk` or confirming CLI prerequisites, use [references/shared-defaults.md](references/shared-defaults.md) and then continue to the product-specific child skill.
- If the user asks for both Hermes and OpenClaw in the same turn, present a two-part plan and execute them as separate product workflows.

## Reference Map

- [references/prerequisites.md](references/prerequisites.md): required credentials and configuration methods
- [references/routing.md](references/routing.md): intent routing and child-skill selection
- [references/shared-defaults.md](references/shared-defaults.md): shared defaults for install, regions, naming, batch output
