# Routing

Use this file to decide which child skill should own the request.

## Hermes Route

Choose `agentengine-hermes-lifecycle` when the user asks to:

- install or use `agentengine hermes`
- run `agentengine init -f hermes`
- deploy Hermes
- check Hermes status
- open Hermes management UI or hosted chat UI
- generate permanent `/chat` and `/` links
- use `agentengine invoke` for Hermes
- run `agentengine hermes connect`
- run `agentengine hermes pairing`
- batch-create Hermes agents

Typical requests:

- "帮我创建 3 个 Hermes agent 并给永久链接"
- "部署一个 Hermes 并打开管理 UI"
- "帮我跑 hermes connect"
- "给我 Hermes 的永久 chat 链接和管理链接"
- "用 agentengine invoke 连一下 Hermes"
- "帮我做 Hermes pairing 审批"

## OpenClaw Route

Choose `agentengine-openclaw-oneclick-deploy` when the user asks to:

- install or use `agentengine openclaw`
- deploy OpenClaw
- list or inspect OpenClaw status
- create dashboard links for OpenClaw
- run `agentengine openclaw channel connect`
- batch-create OpenClaw instances

Typical requests:

- "帮我一键部署 OpenClaw 并给 dashboard 链接"
- "批量创建 2 个 OpenClaw"
- "帮我做微信或飞书 channel connect"
- "给这个 OpenClaw 创建永久不过期的访问地址"
- "帮我看一下 OpenClaw 状态"
- "帮我把 OpenClaw 接到微信"

## Mixed Requests

If the user mixes both products:

1. present a short split plan
2. run one product workflow at a time
3. keep the output grouped by product

Do not share generated IDs, working directories, or dashboard links across products.
