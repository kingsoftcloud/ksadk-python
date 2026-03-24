# ksadk (AgentEngine CLI)

`ksadk` 是金山云 Agent 开发与部署工具链，提供统一的 CLI 体验，覆盖本地开发、构建、部署、调用、版本管理、MCP Server 管理，以及 OpenClaw Gateway / Channel 运维接入。

当前版本：`0.3.6`

## 核心能力

- 多框架支持：DeepAgents、LangGraph、LangChain、Google ADK。
- ADK 增强能力：支持短期/长期记忆体（STM/LTM）与知识库工具注入。
- 本地开发：`run`（API/TUI）与 `web`（本地 Invoke 调试 UI）。
- 云端工作流：`build`、`deploy`、`launch`，支持 `Code` / `Container` 两种制品模式。
- 统一资源命令：`agent`、`mcp`、`openclaw`、`version`、`dashboard` 采用统一的 `list/status/delete/open` 语义。
- 双一等用户体验：默认 `pretty` 输出面向人类，`--output json` 为 AI Agent / 自动化调用提供稳定结构化契约。
- 可调试 Dry Run：`build/deploy/launch --dry-run` 会输出本地执行计划、远端请求摘要和完整 `curl`。
- 统一控制面：通过 `AgentEngine Server` 进行 Agent/MCP/OpenClaw 管理。
- 状态持久化：部署后保存 `.agentengine.state`，供后续 `agent status`、`agent delete`、`version`、`dashboard open`、`openclaw channel/gateway` 等命令自动解析。
- 版本管理：`version list/release/rollback`。
- MCP 管理：`mcp build/deploy/list/status/delete`。

## 安装

```bash
pip install -U ksadk
```

可选依赖：

```bash
pip install "ksadk[langgraph]"
pip install "ksadk[langchain]"
pip install "ksadk[deepagents]"
pip install "ksadk[adk]"
pip install "ksadk[kb]"
```

安装后可使用以下命令入口（等价）：

```bash
agentengine --help
ksadk --help
```

## 快速开始

### 1) 初始化项目

```bash
agentengine init my_agent -f langgraph
cd my_agent
```

DeepAgents 模板：

```bash
agentengine init my_deep_agent -f deepagents
cd my_deep_agent
```

OpenClaw 模板：

```bash
agentengine init my_claw -f openclaw
cd my_claw
agentengine openclaw deploy
```

也可包装已有代码：

```bash
agentengine init --from-agent ./my_agent.py
agentengine init --from-agent ./my_agent_dir
```

### 2) 配置项目

交互式向导：

```bash
agentengine config
```

非交互式查看/修改：

```bash
agentengine config show
agentengine config set region=cn-beijing-6 OPENAI_MODEL_NAME=glm-5
```

会生成或更新：

- `agentengine.yaml`
- `.env`

### 3) 本地调试

```bash
agentengine run -i
```

或启动 Web UI：

```bash
agentengine web --port 8080
```

### 4) 一键构建+部署

```bash
export KSYUN_ACCESS_KEY=your-ak
export KSYUN_SECRET_KEY=your-sk
export KSYUN_ACCOUNT_ID=your-account-id
export KSYUN_REGION=cn-beijing-6

agentengine launch . --target serverless
```

### 5) 打开云端已部署 Agent UI

```bash
# 目录内自动解析 agent（.agentengine.state -> agentengine.yaml/ksadk.yaml）
agentengine dashboard open

# 显式指定 Agent
agentengine dashboard open --agent ar-xxxx

# OpenClaw 也走统一入口
agentengine dashboard open --agent openclaw-gateway-xxxx

# 在 OpenClaw 工作目录内也可直接无参打开（自动读取 .agentengine.state）
agentengine dashboard open

# 创建可分享链接（默认打开浏览器）
agentengine dashboard open --agent ar-xxxx --share --expires-seconds 86400

# 仅输出 URL，不自动打开
agentengine dashboard open --agent ar-xxxx --no-open

# 查看/撤销分享链接
agentengine dashboard share list --agent ar-xxxx
agentengine dashboard share revoke <link_id> --yes
```

### 6)（可选）启用 ADK 记忆与知识库

```bash
# 记忆体后端: local | http | sdk
export KSADK_LTM_BACKEND=local

# 配置知识库后，Runner 会自动注入 search_knowledge_base 工具
export KSADK_KB_DATASET_ID=your_dataset_id
```

## 命令总览

- `agentengine init`：创建新项目（支持 `--from-agent`）。
- `agentengine config`：进入交互式配置向导。
- `agentengine config show`：查看项目配置、全局配置与当前生效环境变量。
- `agentengine config set KEY=VALUE...`：非交互式更新配置。
- `agentengine config model`：交互式切换默认模型。
- `agentengine run`：本地运行 Agent（支持 `-i` TUI）。
- `agentengine web`：启动本地调试 UI（ADK 项目用 ADK Web，其他用 Chainlit）。
- `agentengine build`：构建制品（`code` 或 `container`）。
- `agentengine deploy`：部署到 `serverless` / `kcf` / `kce`。
- `agentengine launch`：`build + deploy` 一条命令完成。
- `agentengine agent list|status|invoke|delete`：远端 Agent 资源管理。
- `agentengine dashboard open`：打开云端已部署 Agent 的 Dashboard/WebUI（默认创建 `/s/{link_id}` 短链接）。
- `agentengine dashboard share list|revoke`：管理 Dashboard 分享链接。
- `agentengine version list|release|rollback`：Agent 版本管理。
- `agentengine mcp build|list|status|deploy|delete`：MCP Server 管理。
- `agentengine openclaw list|status|deploy|delete`：OpenClaw 资源管理。
- `agentengine openclaw gateway open|ws-url|logs|doctor`：OpenClaw Gateway 打开、诊断与日志。
- `agentengine openclaw channel status|connect|enable|disable|doctor`：OpenClaw 渠道接入与诊断。
- `agentengine completion`：Shell 补全脚本与自动安装。

说明：
- 文档只展示 canonical 命令；旧的 `status` / `delete` / `destroy` / `dashboard [agent_ref]` 等入口仍保留兼容，但不再推荐。
- `dashboard open` 默认通过 `CreateDashboardAccessLink` 生成短链接并打开浏览器。
- `dashboard share revoke`、`agent delete`、`mcp delete`、`openclaw delete` 在自动化场景下建议显式传 `--yes`。

## AI Agent / 自动化调用

核心云端命令支持统一的机器输出：

```bash
agentengine agent status --agent ar-xxxx --output json
agentengine deploy --target serverless --dry-run --output json
agentengine config show --output json
```

约定如下：

- 默认输出为 `pretty`，适合人类阅读。
- `--output json` 返回稳定 envelope，适合 AI Agent / CI / 脚本解析。
- destructive 命令在 JSON 模式下必须显式传 `--yes`。
- `dashboard open --output json` 不会自动打开浏览器，等价于 `--no-open`。
- 交互式命令如 `config wizard`、`run`、`web` 不建议作为自动化入口；非交互推荐使用 `config show` / `config set`。

## Agent 指定规则（统一）

适用于：`agent status`、`agent invoke`、`agent delete`、`version`、`dashboard open` 子命令；OpenClaw 目录下的 `dashboard open` 也会自动读取 `.agentengine.state`。

支持三种写法：

- 推荐：`--agent <id-or-name>`
- 兼容：`--agent-id <id-or-name>`
- 位置参数：`<id-or-name>`

示例：

```bash
agentengine agent status --agent ar-xxxx
agentengine agent status ar-xxxx
agentengine agent invoke --agent my_agent -m "你好"
agentengine agent delete my_agent --yes
agentengine version list --agent ar-xxxx
agentengine dashboard open --agent ar-xxxx
```

未显式传 Agent 时，自动解析顺序为：

1. `.agentengine.state`（优先 `agent_id`，其次 `name`）
2. `agentengine.yaml` / `ksadk.yaml` 的 `name`

## OpenClaw 统一入口

常用命令：

```bash
# 从空目录初始化一个最小 OpenClaw 项目
agentengine init my_claw -f openclaw

# 部署 / 状态 / 打开 Dashboard
agentengine openclaw deploy
agentengine openclaw status
agentengine dashboard open

# Gateway 诊断
agentengine openclaw gateway doctor
agentengine openclaw gateway ws-url

# 渠道状态与扫码接入
agentengine openclaw channel status --probe
agentengine openclaw channel connect --channel weixin
agentengine openclaw channel connect --channel feishu
```

行为说明：

- OpenClaw 默认镜像会预置 `openclaw-weixin`、`openclaw-lark`、`agent-browser` 与 `skillhub` CLI。
- 同一镜像也会预装常用检索/脚本依赖（如 `curl`、`jq`、`python3`），避免默认搜索/JSON 解析链路因基础工具缺失而失败。
- 镜像默认固定安装 `@tencent-weixin/openclaw-weixin@2.0.1`。该版本已原生兼容当前 OpenClaw `plugin-sdk` / runtime 加载；但当前 host 侧仍不会自动暴露 `web.login.start/web.login.wait`，所以启动时仅额外补一层极小 shim，方便 `agentengine openclaw channel connect --channel weixin` 走统一入口远端扫码。
- 默认 bundled skills 为 `skillhub-store`、`agent-browser-clawdbot`、`kdocs`。
- 默认搜索主路径恢复为 OpenClaw 原生内建 `browser`；`agent-browser` 仍作为增强/备选能力随镜像预装，适合需要更强 CLI 可控性、会话隔离或可重放步骤的网页自动化任务。`multi-search-engine` 仍保留在镜像内作为可选技能，但不再默认同步到用户目录。需要轻量文本检索兜底时，可显式把它加入 `OPENCLAW_PRESET_SKILLS_ALLOWLIST`，例如 `skillhub-store,agent-browser-clawdbot,kdocs,multi-search-engine`。
- 旧默认技能如 `find-skills` 仅会在“此前由镜像同步且用户未改动”的情况下被自动迁移移除；用户自己改过的同名技能目录会被保留。
- 镜像默认开启 OpenClaw 内建 `browser`（headless Chromium），并修复 fresh deploy 下 `127.0.0.1` loopback 调用误入 pairing 的问题；如需显式关闭，可设置 `OPENCLAW_BROWSER_ENABLED=false`。
- 微信 `channel connect` 在有 TTY 时默认打印终端二维码；传 `--open-qr` 会额外打开本地浏览器。
- 飞书 `channel connect` 会启动官方 onboarding 流程，并把扫码/配置结果写回远端实例。

## 构建与部署

### build

```bash
# 1) 默认构建 (code 模式)
agentengine build .

# 2) 显式指定构建参数
agentengine build . --mode container --push --registry hub-cn-beijing-6.kce.ksyun.com
# 3) 显式指定区域
KSYUN_REGION=cn-beijing-6 agentengine build . --mode code --push --no-cache
```

说明：

- `build` 支持 `--output pretty|json`。
- `--no-cache` 会强制重建，不复用已缓存的本地制品元数据。

### deploy

```bash
# 1) 默认部署 (serverless)
agentengine deploy .
# 2) 显式指定部署参数
agentengine deploy . --target kcf --account-id X-Ksc-Account-Id
# 3) 显式指定区域
KSYUN_REGION=cn-beijing-6 agentengine deploy . --target serverless --dry-run
```

Dry Run 会输出两层计划：

- 本地计划：校验、打包、`local_build`、`artifact_publish`、`deploy_request`
- 远端请求：请求方法、URL、字段摘要以及完整 `curl`

常用参数：

- `--artifact-type [Code|Container]`
- `--region`
- `--account-id`
- `--observability/--no-observability`
- `--no-version`
- `--auto-rollback`
- `--output [pretty|json]`
- `--dry-run`
- `--no-cache`

### launch

```bash
# 1) 默认一键部署 (serverless)
agentengine launch .
# 2) 显式指定部署参数
agentengine launch . --target kce --artifact-type Container
# 3) 显式指定区域
KSYUN_REGION=cn-beijing-6 agentengine launch . --target serverless --no-cache
```

`launch --dry-run` 与 `deploy --dry-run` 一样，会同时展示本地执行计划和远端请求 `curl`，便于排查构建复用、上传与部署参数。

## 版本管理

```bash
# 1) 目录内自动解析 agent（优先 .agentengine.state）
agentengine version list

# 2) 显式指定 agent
agentengine version list --agent ar-xxxx
agentengine version release --agent ar-xxxx --tag v1.0.1 --description "release note"
agentengine version rollback --agent ar-xxxx --to v1.0.0 -y

# 3) 显式指定区域
KSYUN_REGION=cn-beijing-6 agentengine version list --agent ar-xxxx
KSYUN_REGION=cn-beijing-6 agentengine version release --agent ar-xxxx --tag v1.0.1
KSYUN_REGION=cn-beijing-6 agentengine version rollback --agent ar-xxxx --to v1.0.0 -y
```

## MCP Server 管理

`agentengine mcp` 用于部署 FastMCP 项目并输出标准 MCP endpoint（`<endpoint>/mcp`）。

### 1) 支持命令

- `agentengine mcp build [MCP_DIR]`：构建 MCP 制品（`Code` / `Container`）。
- `agentengine mcp deploy [MCP_DIR]`：部署或热更新 MCP。
- `agentengine mcp list`：列出 MCP 列表。
- `agentengine mcp status <mcp_id>`：查看 MCP 详情。
- `agentengine mcp delete <mcp_id>`：删除 MCP（可配 `--yes` 跳过确认）。

### 2) 项目检测规则

CLI 自动识别 FastMCP 项目，命中任一条件即可：

- 配置文件声明：`agentengine.yaml` / `ksadk.yaml` / `mcp.yaml` 中 `type: mcp` 或 `framework: mcp`。
- 代码特征：存在 `from fastmcp import FastMCP`（或等价导入）。

并会尽量提取：

- 入口文件（entry point）
- MCP 实例变量名（默认 `mcp`）
- `@mcp.tool` 对应的工具列表

### 3) 部署参数

```bash
agentengine mcp deploy [MCP_DIR] \
  [--name NAME] \
  [--region REGION] \
  [--artifact-type Code|Container] \
  [--ks3-bucket BUCKET] \
  [--enable-auth] \
  [--no-cache] \
  [--dry-run]
```

- `MCP_DIR`：项目目录，默认当前目录。
- `--name`：实例名，默认目录名（会将 `-`、`.` 归一化为 `_`）。
- `--region`：部署区域，默认 `cn-beijing-6`（可由 `KSYUN_REGION` 覆盖）。
- `--artifact-type`：`Code`（默认）或 `Container`。
- `--ks3-bucket`：Code 模式自定义 bucket。
- `--enable-auth`：启用 API Key 保护（默认关闭）。
- `--no-cache`：强制重建。
- `--dry-run`：仅打印请求，不实际执行。

### 4) 当前实现说明（与代码一致）

#### Code 模式（推荐，当前最稳定）

- 流程：打包代码 -> 上传 KS3 -> 调用 `CreateMCP/UpdateMCP`。
- MCP 入口会自动生成 `entrypoint.py`，以 HTTP transport 启动 FastMCP。
- 运行时支持通过 `<endpoint>/mcp` 接入外部客户端。

#### Container 模式（可用）

- CLI 会执行本地 Docker build + push，并将镜像地址作为 `ArtifactPath` 提交。
- FastMCP 项目会走专用的 MCP 容器构建链路，不再复用 Agent 框架探测。
- CLI 会向服务端透传 `DeploymentType=Container`、`ContainerConfig` 以及镜像仓库凭证。
- `--dry-run` 当前仅跳过最终的 `CreateMCP/UpdateMCP` 请求，本地 build/push 仍会执行。

### 5) 典型流程

```bash
# 1) 先构建（可选）
agentengine mcp build . --artifact-type Code --push

# 2) 部署
agentengine mcp deploy .

# 3) 查询
agentengine mcp list
agentengine mcp status <mcp_id>

# 4) 删除
agentengine mcp delete <mcp_id> --yes
```

### 6) 本地状态与热更新

部署目录会保存 `.agentengine.state`（`type: mcp`），包含 `mcp_id`、`endpoint`、`mcp_endpoint` 等信息。

- 存在 `mcp_id`：`mcp deploy` 走热更新（`update_mcp`）。
- 不存在 `mcp_id`：走新建（`create_mcp`）。
- 删除成功后会尝试清理当前目录匹配的状态文件。

### 7) 环境与凭证

- 控制面调用所需：`KSYUN_ACCESS_KEY`、`KSYUN_SECRET_KEY`（建议同时配置 `KSYUN_ACCOUNT_ID`）。
- `status` / `delete` 当前实现会检查 `AGENTENGINE_SERVER_URL`，未配置会直接报错。

### 8) 接入示例

部署成功后可使用：

- Cursor / Claude Code：`{"url": "<endpoint>/mcp"}`
- LangChain/LangGraph：`MCPClientToolkit(url="<endpoint>/mcp")`
- Google ADK：`MCPToolset.from_server(connection_params={"url": "<endpoint>/mcp"})`

### 9) 常见问题

- `mcp deploy` 提示未检测到 FastMCP 项目：检查导入语句或配置文件声明。
- `status/delete` 提示未配置 `AGENTENGINE_SERVER_URL`：先设置服务端地址再执行。
- `Container` 失败：确认 Docker 可用；如涉及私有镜像，确认已配置仓库凭证并检查 `ContainerConfig` / 镜像地址是否符合目标环境要求。

## 关键文件

- `agentengine.yaml`：项目配置（name/framework/entry_point 等）。
- `.env`：模型、云凭证、可观测性配置。
- `.agentengine.state`：部署后本地状态（`agent_id` / `endpoint` / `api_key` / `region`）。
- `~/.agentengine/settings.json`：全局配置（可被 `agentengine config set --global` 或配置向导更新）。

## 环境变量

| 变量名 | 说明 |
|---|---|
| `OPENAI_API_KEY` | 模型 API Key |
| `OPENAI_BASE_URL` | 模型 API Base URL |
| `OPENAI_MODEL_NAME` | 模型名称 |
| `KSYUN_ACCESS_KEY` | 金山云 AK |
| `KSYUN_SECRET_KEY` | 金山云 SK |
| `KSYUN_ACCOUNT_ID` | 金山云账号 ID |
| `KSYUN_REGION` | 默认区域 |
| `LANGFUSE_PUBLIC_KEY` | Langfuse 公钥 |
| `LANGFUSE_SECRET_KEY` | Langfuse 私钥 |
| `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` | Langfuse 地址 |
| `KSADK_LTM_BACKEND` | ADK 长期记忆后端（`local/http/sdk`） |
| `KSADK_LTM_HTTP_URL` | LTM HTTP 后端地址（当 `KSADK_LTM_BACKEND=http`） |
| `KSADK_KB_DATASET_ID` | 知识库 Dataset ID（配置后启用知识库检索） |

兼容别名仍可识别：`OPENAI_API_BASE`、`MODEL_NAME`。

## 架构说明

云端链路：

```text
CLI (ksadk) -> AgentEngine Server (控制面) -> Serverless/KCF/KCE
```

本地链路：

```text
CLI (run/web) -> Unified Runner -> 本地 Agent 进程
```

## 补全

```bash
agentengine completion install --shell auto
```

说明：

- 当前自动安装支持 `zsh` 和 `bash`。
- Linux / macOS / Git Bash / WSL 均可直接使用；`--shell auto` 会优先根据当前 shell 与运行环境选择合适的配置文件。
- `bash` 下默认会优先写入当前环境更常用的 profile：macOS 倾向 `~/.bash_profile`，Linux / Git Bash / WSL 倾向 `~/.bashrc`。
- 安装时会自动清理旧的 `eval "$(_AGENTENGINE_COMPLETE=...)"` 片段与重复 `source` 行，减少补全配置漂移。
- PowerShell 暂未内建自动安装。

## 示例项目

见 [examples](./examples) 目录。

## 进阶文档

- [DeepAgents 框架支持说明](./docs/deepagents.md)
- [ADK 记忆能力使用说明](./docs/memory_usage_guide.md)
- [知识库与记忆能力示例](./docs/knowledge_base_and_memory_examples.md)
