# ksadk (AgentEngine CLI)

`ksadk` 是金山云 Agent 开发与部署工具链，提供统一的 CLI 体验，覆盖本地开发、构建、部署、调用、版本管理与 MCP Server 管理。

当前版本：`0.2.0`

## 核心能力

- 多框架支持：DeepAgents、LangGraph、LangChain、Google ADK。
- 本地开发：`run`（API/TUI）与 `web`（Web UI）。
- 云端部署：`build`、`deploy`、`launch`，支持 `Code` / `Container` 两种制品模式。
- 统一控制面：通过 `AgentEngine Server` 进行 Agent/MCP 管理。
- 状态持久化：部署后保存 `.agentengine.state`，供后续 `status/invoke/destroy/version` 复用。
- 版本管理：`version list/release/rollback`。
- MCP 管理：`mcp deploy/list/status/delete`。

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

也可包装已有代码：

```bash
agentengine init --from-agent ./my_agent.py
agentengine init --from-agent ./my_agent_dir
```

### 2) 交互式配置

```bash
agentengine config
```

会生成或更新：

- `agentengine.yaml`
- `.env`

### 3) 本地调试

```bash
agentengine run . -i
```

或启动 Web UI：

```bash
agentengine web . --port 8080
```

### 4) 一键构建+部署

```bash
export KSYUN_ACCESS_KEY=your-ak
export KSYUN_SECRET_KEY=your-sk
export KSYUN_ACCOUNT_ID=your-account-id
export KSYUN_REGION=cn-beijing-6

agentengine launch . --target serverless
```

## 命令总览

- `agentengine init`：创建新项目（支持 `--from-agent`）。
- `agentengine config`：交互式配置 `agentengine.yaml` + `.env`。
- `agentengine model`：从模型服务拉取模型列表并更新 `.env` 的 `OPENAI_MODEL_NAME`。
- `agentengine run`：本地运行 Agent（支持 `-i` TUI）。
- `agentengine web`：启动 Web UI（ADK 项目用 ADK Web，其他用 Chainlit）。
- `agentengine build`：构建制品（`code` 或 `container`）。
- `agentengine deploy`：部署到 `serverless` / `kcf` / `kce`。
- `agentengine launch`：`build + deploy` 一条命令完成。
- `agentengine status`：查看运行状态与 endpoint。
- `agentengine invoke`：调用远端或本地 Agent。
- `agentengine destroy`：销毁 Agent。
- `agentengine version`：版本管理（`list/release/rollback`）。
- `agentengine mcp`：MCP Server 管理。
- `agentengine completion`：Shell 补全脚本与自动安装。

## Agent 指定规则（统一）

适用于：`status`、`invoke`、`destroy`、`version` 子命令。

支持三种写法：

- 推荐：`--agent <id-or-name>`
- 兼容：`--agent-id <id-or-name>`
- 位置参数：`<id-or-name>`

示例：

```bash
agentengine status --agent ar-xxxx
agentengine status ar-xxxx
agentengine invoke --agent my_agent -m "你好"
agentengine destroy my_agent
agentengine version list --agent ar-xxxx
```

未显式传 Agent 时，自动解析顺序为：

1. `.agentengine.state`（优先 `agent_id`，其次 `name`）
2. `agentengine.yaml` / `ksadk.yaml` 的 `name`

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

### deploy

```bash
# 1) 默认部署 (serverless)
agentengine deploy .
# 2) 显式指定部署参数
agentengine deploy . --target kcf --account-id X-Ksc-Account-Id
# 3) 显式指定区域
KSYUN_REGION=cn-beijing-6 agentengine deploy . --target serverless --dry-run
```

常用参数：

- `--artifact-type [Code|Container]`
- `--region`
- `--account-id`
- `--observability/--no-observability`
- `--no-version`
- `--auto-rollback`

### launch

```bash
# 1) 默认一键部署 (serverless)
agentengine launch .
# 2) 显式指定部署参数
agentengine launch . --target kce --artifact-type Container
# 3) 显式指定区域
KSYUN_REGION=cn-beijing-6 agentengine launch . --target serverless --no-cache
```

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

```bash
# 1) 默认部署
agentengine mcp deploy .
# 2) 常用查询
agentengine mcp list
# 3) 显式指定区域
KSYUN_REGION=cn-beijing-6 agentengine mcp status <mcp_id>
# 删除
agentengine mcp delete <mcp_id> --yes
```

## 关键文件

- `agentengine.yaml`：项目配置（name/framework/entry_point 等）。
- `.env`：模型、云凭证、可观测性配置。
- `.agentengine.state`：部署后本地状态（`agent_id` / `endpoint` / `api_key` / `region`）。
- `~/.agentengine/settings.json`：全局配置（可被 `config --global` 更新）。

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

## 示例项目

见 [examples](./examples) 目录。

## 进阶文档

- [DeepAgents 框架支持说明](./docs/deepagents.md)
