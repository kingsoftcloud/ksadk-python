# KSADK 使用文档

本文档是 `ksadk` 当前对外主使用文档，覆盖本地开发、构建部署、远端资源管理、MCP、OpenClaw、Hermes、平台级 KB/LTM 以及自动化输出约定。

当前版本：`0.4.0`

## 1. 安装与环境准备

### 1.1 安装

```bash
pip install -U ksadk
```

可选 extras：

```bash
pip install "ksadk[langgraph]"
pip install "ksadk[langchain]"
pip install "ksadk[deepagents]"
pip install "ksadk[adk]"
pip install "ksadk[kb]"
```

命令入口等价：

```bash
agentengine --help
ksadk --help
```

### 1.2 常用环境变量

最常用的是两类配置：

- 模型调用配置
- 金山云凭证 / 区域配置

常用环境变量：

| 变量 | 用途 |
|------|------|
| `OPENAI_API_BASE` / `OPENAI_API_KEY` / `MODEL_NAME` | 本地运行和调试时的模型调用配置 |
| `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY` | 构建、部署、KB/LTM、OpenClaw 等金山云能力 |
| `KSYUN_ACCOUNT_ID` | 远端资源创建和部署 |
| `KSYUN_REGION` | 默认区域 |

如果启用平台级 KB / LTM，再额外配置：

- `KSADK_KB_*`
- `KSADK_LTM_*`

具体语义见本文第 5 节。

## 2. 快速开始

### 2.1 初始化项目

LangGraph 示例：

```bash
agentengine init my_agent -f langgraph
cd my_agent
```

DeepAgents 示例：

```bash
agentengine init my_deep_agent -f deepagents
cd my_deep_agent
```

OpenClaw 示例：

```bash
agentengine init my_claw -f openclaw
cd my_claw
```

Hermes 示例：

```bash
agentengine init my_hermes -f hermes
cd my_hermes
```

也可以包装已有代码：

```bash
agentengine init --from-agent ./my_agent.py
agentengine init --from-agent ./my_agent_dir
```

### 2.2 配置项目

交互式向导：

```bash
agentengine config
```

查看或修改：

```bash
agentengine config show
agentengine config set region=cn-beijing-6 OPENAI_MODEL_NAME=glm-5.1
```

常见配置文件：

- `agentengine.yaml`
- `.env`

### 2.3 本地运行

交互式运行：

```bash
agentengine run -i
```

启动本地统一 Web UI：

```bash
agentengine web --port 8080
```

说明：

- `run` 适合终端交互调试
- `web` 适合本地 Invoke UI 调试
- `web` 是本地 UI，不是云端 Dashboard
- 所有受支持框架当前统一使用 ksadk 内建 Web UI

### 2.4 一键部署代码框架并打开云端 UI

```bash
export KSYUN_ACCESS_KEY=your-ak
export KSYUN_SECRET_KEY=your-sk
export KSYUN_ACCOUNT_ID=your-account-id
export KSYUN_REGION=cn-beijing-6

agentengine launch . --target serverless
agentengine dashboard open
```

这条路径适用于 LangGraph / LangChain / DeepAgents / ADK / OpenClaw 等本地项目构建型框架。

### 2.5 Hermes 云端生命周期

Hermes 走共享 runtime 镜像，不要求用户先本地 build/push：

```bash
agentengine init my_hermes -f hermes
cd my_hermes

agentengine hermes deploy --name my-hermes
agentengine hermes status my-hermes
agentengine invoke my-hermes
agentengine hermes open my-hermes --chat
agentengine hermes exec my-hermes -- status
agentengine hermes pairing my-hermes -- list
agentengine hermes open my-hermes --manage
```

说明：

- `agentengine invoke <hermes-agent>` 默认进入 Hermes 原生远程 TUI。
- `agentengine invoke <hermes-agent> -m "hello"` 继续走 `/v1/chat/completions`。
- `agentengine hermes open <hermes-agent> --chat` 打开统一 hosted chat 页面。
- `agentengine hermes exec` 只允许只读运维子命令，不是远程 shell。
- `agentengine hermes pairing` 只透传 Hermes 原生 pairing 审批子命令。
- 当 `OPENAI_BASE_URL` 指向 `kspmas.ksyun.com` 公网模型网关时，`agentengine hermes deploy` 会在云端 runtime 配置里自动改写成 `http://kspmas-internal.sdns.ksyun.com/v1`，避免 Pod 访问公网模型网关超时。

## 3. 日常开发主线

### 3.1 本地调试

最常见的本地调试路径是：

1. `agentengine init`
2. `agentengine config`
3. `agentengine run -i`
4. 需要 UI 时使用 `agentengine web`

如果要覆盖模型：

```bash
agentengine web --model glm-5.1
```

### 3.2 构建

```bash
agentengine build .
```

常用场景：

```bash
agentengine build . --mode code
agentengine build . --mode container
agentengine build . --dry-run
```

当前实现支持 `Code` 与 `Container` 两类制品，适合作为 `deploy` 或 `launch` 的前置验证。

Hermes 不走这条本地 build 主线；它默认消费平台共享的 Hermes runtime 镜像。

### 3.3 部署

```bash
agentengine deploy . --target serverless
```

常见目标：

- `serverless`
- `kcf`
- `kce`

示例：

```bash
agentengine deploy . --target serverless --dry-run
agentengine deploy . --target kcf --account-id X-Ksc-Account-Id
```

Hermes 例外：

```bash
agentengine hermes deploy --name demo-hermes
```

Hermes deploy 直接调用控制面 `CreateAgentProduct` / `UpdateAgent`，使用共享 runtime 镜像，不依赖本地 `build` 产物。

### 3.4 Launch

`launch` 是 `build + deploy` 的主入口：

```bash
agentengine launch . --target serverless
```

适合：

- 从本地项目直接发起一键部署
- 想保留统一的 dry-run / JSON 输出 / 状态回填语义

Hermes 不推荐走 `launch`；它有独立的 `agentengine hermes deploy/status/open/delete/exec` 生命周期命令组。

## 4. 远端资源管理

### 4.1 Agent 与 Version

常用命令：

```bash
agentengine agent list
agentengine agent status --agent ar-xxxx
agentengine agent invoke --agent ar-xxxx -m "你好"
agentengine agent delete --agent ar-xxxx --yes

agentengine version list --agent ar-xxxx
agentengine version release --agent ar-xxxx
agentengine version rollback --agent ar-xxxx --version v1
```

### 4.2 Hermes

Hermes 是带原生远程 TUI 的一等公民资源组：

```bash
agentengine hermes list
agentengine hermes status ar-xxxx
agentengine hermes open ar-xxxx
agentengine hermes exec ar-xxxx -- doctor
agentengine hermes pairing ar-xxxx -- list
agentengine hermes delete ar-xxxx -y
agentengine invoke ar-xxxx
agentengine hermes open ar-xxxx --chat
```

### 4.2 Dashboard

打开云端 UI：

```bash
agentengine dashboard open
agentengine dashboard open --agent ar-xxxx
agentengine dashboard open --agent ar-xxxx --share --expires-seconds 86400
agentengine dashboard open --agent ar-xxxx --no-open
agentengine dashboard open --agent ar-xxxx --direct
```

分享链接管理：

```bash
agentengine dashboard share list --agent ar-xxxx
agentengine dashboard share revoke <link_id> --yes
```

说明：

- `dashboard open` 默认会创建 Dashboard 短链接
- `--no-open` 只打印 URL
- `--direct` 直接打开 endpoint/path，跳过短链接创建
- OpenClaw 也走同一个 `dashboard open` 入口

### 4.3 MCP

MCP 资源管理：

```bash
agentengine mcp build .
agentengine mcp deploy .
agentengine mcp list
agentengine mcp status <mcp_id>
agentengine mcp delete <mcp_id> --yes
```

当前实现会自动识别 FastMCP 项目。MCP 项目入口需要满足配置声明或代码特征识别。

### 4.4 OpenClaw

OpenClaw 统一入口：

```bash
agentengine openclaw deploy
agentengine openclaw status
agentengine dashboard open
agentengine openclaw gateway doctor
agentengine openclaw channel status --probe
agentengine openclaw channel connect --channel weixin
agentengine openclaw channel connect --channel feishu
```

OpenClaw 工作目录内可直接运行 `agentengine dashboard open`，会自动读取当前目录 `.agentengine.state` 中的 OpenClaw 实例引用。

## 5. 平台级能力：KB / LTM

### 5.1 基本语义

平台级能力分两类：

- `KSADK_KB_*`：知识库
- `KSADK_LTM_*`：长期记忆

当前支持两种接入路径：

1. ADK native path
2. LangChain / LangGraph / DeepAgents ambient path

### 5.2 ADK native path

ADK 项目只配环境变量即可自动获得：

- `search_knowledge_base`
- `load_memory`
- `save_memory`

也就是说，ADK 下默认是“runner 自动注入工具”。

### 5.3 Ambient path

LangChain / LangGraph / DeepAgents 默认走平台 ambient path：

- 默认策略：`on_demand`
- 只在需要外部知识或长期记忆时注入上下文
- 不需要显式改 agent 代码

如果希望改成始终加载：

```bash
export KSADK_KB_AMBIENT_POLICY=always
export KSADK_LTM_AMBIENT_POLICY=always
```

如果希望显式关闭：

```bash
export KSADK_KB_AMBIENT_ENABLED=false
export KSADK_LTM_AMBIENT_ENABLED=false
```

### 5.4 常见配置

示例：

```ini
# 模型
OPENAI_API_KEY=your-api-key
OPENAI_API_BASE=http://kspmas.ksyun.com/v1
MODEL_NAME=deepseek-v3.2

# 全局 AK/SK
KSYUN_ACCESS_KEY=your-ak
KSYUN_SECRET_KEY=your-sk
KSYUN_REGION=cn-beijing-6

# 知识库
KSADK_KB_DATASET_ID=your_kb_id
KSADK_KB_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_KB_SCHEME=http

# 长期记忆
KSADK_LTM_BACKEND=sdk
KSADK_LTM_NAMESPACE=your_namespace
KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_LTM_SCHEME=http
```

默认规则：

- `KSADK_KB_REGION` / `KSADK_LTM_REGION` 未设置时回退到 `KSYUN_REGION`
- endpoint 是 `*.inner.api.ksyun.com` 且未显式设置 scheme 时，默认使用 `http`

### 5.5 DeepAgents 与平台能力

DeepAgents 当前沿用 LangGraph 路径，因此：

- ambient KB/LTM 语义与 LangGraph 一致
- `DeepAgentsRunner` 复用 LangGraph runner 逻辑
- 详细框架说明见 [deepagents.md](./deepagents.md)

## 6. 自动化与 JSON 输出

核心云端命令支持机器可读输出：

```bash
agentengine agent status --agent ar-xxxx --output json
agentengine deploy --target serverless --dry-run --output json
agentengine config show --output json
agentengine dashboard open --agent ar-xxxx --output json
```

约定：

- 默认输出为 `pretty`
- `--output json` 返回稳定结构化结果
- destructive 命令在自动化场景建议显式传 `--yes`
- `dashboard open --output json` 不会自动打开浏览器，等价于 `--no-open`

### 6.1 Dry Run

`build` / `deploy` / `launch` 支持 `--dry-run`，用于查看：

- 本地执行计划
- 远端请求摘要
- 完整 `curl`

适合用于：

- CI 验证
- 参数排查
- 制品与部署请求确认

## 7. 常见问题

### 7.1 Agent 是怎么自动解析的

适用于：

- `agent status`
- `agent invoke`
- `agent delete`
- `version`
- `dashboard open`

未显式传 agent 时，自动解析顺序：

1. `.agentengine.state`
2. `agentengine.yaml` / `ksadk.yaml` 的 `name`

推荐写法：

```bash
agentengine dashboard open --agent ar-xxxx
```

### 7.2 MCP 项目没有被识别

优先检查：

- 是否是 FastMCP 项目
- 是否有显式配置
- 代码中是否存在 `from fastmcp import FastMCP` 等特征

如果只是要接入已部署 MCP endpoint，而不是部署 MCP 项目，请看技术文档里的 MCP runtime 说明。

### 7.3 Dashboard 为什么默认创建短链接

当前 canonical 行为是：

- `dashboard open` 默认通过 `CreateDashboardAccessLink` 获取访问 URL
- `--share` 管理可分享链接
- `--direct` 才会跳过短链接

### 7.4 OpenClaw 什么时候用独立命令，什么时候用 Dashboard

经验规则：

- 实例生命周期：用 `agentengine openclaw ...`
- 云端 UI：用 `agentengine dashboard open`
- 渠道与网关诊断：用 `agentengine openclaw gateway ...` / `channel ...`

### 7.5 KB/LTM 为什么没有自动生效

优先检查：

- 环境变量是否完整
- ADK 是否走了 native path
- 非 ADK 是否因为 `on_demand` 策略没有命中触发条件
- 是否被显式设置为 `*_AMBIENT_ENABLED=false`

更细的 ADK 记忆专项说明见 [memory_usage_guide.md](./memory_usage_guide.md)。

## 8. 进阶参考

以下文档保留为专项 reference：

- [ksadk_technical_design.md](./ksadk_technical_design.md)
- [deepagents.md](./deepagents.md)
- [knowledge_base_and_memory_examples.md](./knowledge_base_and_memory_examples.md)
- [memory_usage_guide.md](./memory_usage_guide.md)
- [openclaw_client_one_click_deploy.md](./openclaw_client_one_click_deploy.md)
- [Runner_Approval_Architecture.md](./Runner_Approval_Architecture.md)
