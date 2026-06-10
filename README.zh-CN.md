# KsADK

[简体中文（默认）](README.md) | [English](README.en.md)

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/kingsoftcloud/ksadk-python)

一次构建 Agent，到处运行。

KsADK 是面向 AI Agent 的运行时平台（Agent Runtime Platform）。你可以继续使用 Google ADK、LangGraph、LangChain 或 DeepAgents 编写业务 Agent，再用 KsADK 获得统一的本地运行、浏览器调试、OpenAI-Compatible API、沙箱执行、部署和可观测体验。

本仓库默认 README 使用简体中文；英文内容仅作为补充入口维护在 [README.en.md](README.en.md)。

候选版本：`0.6.4`（Unreleased，待用户 review；正式发布版本以 PyPI 和 GitHub Release 为准）。

- 本地开发（Local Development）
- 浏览器调试界面（Browser Debugging UI）
- OpenAI 兼容 API（OpenAI-Compatible API）
- 统一运行时（Unified Runtime）
- 沙箱执行（Sandbox Execution）
- Serverless 部署（Serverless Deployment）
- Hermes 与 OpenClaw 运行时

![KsADK 真实 CLI 截图：agentengine -h](https://kingsoftcloud.github.io/ksadk-python/assets/ksadk-runtime-platform-hero.png)

## 为什么需要 KsADK

大多数 Agent 框架主要解决“如何开发 Agent”。

KsADK 解决“如何运行、调试、部署和观测 Agent”。

它不替换你的框架，而是在框架之上补齐运行时平台层：

- 开发：统一 `agentengine init`、`agentengine config`、`agentengine run`。
- 调试：本地 Web UI、会话、附件、workspace 文件和流式输出。
- 运行：统一 Runner、OpenAI-Compatible API 和多框架入口。
- Sandbox：Skill Runtime、Workspace 和 sandbox backend 的隔离执行边界。
- 部署：Serverless、Hermes、OpenClaw 和远端 AgentEngine 入口。
- 可观测：OpenTelemetry-first tracing，可对接多种观测后端。

继续使用你熟悉的框架，同时获得完整的运行时平台能力。

## 30 秒快速体验

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U "ksadk[all]"

agentengine init demo-agent -f langgraph
cd demo-agent
agentengine config set OPENAI_API_KEY=your-api-key OPENAI_MODEL_NAME=gpt-4o-mini
agentengine run -i
```

打开本地浏览器调试界面：

```bash
agentengine web . --no-open
```

下面是脚本生成的真实本地 Web UI 演示：使用 deterministic LangGraph Runner，不连接外部模型或云环境，但完整走本地 FastAPI、Responses streaming、工具调用、思考过程和会话状态链路。

![KsADK 真实 Web UI 调试截图](https://kingsoftcloud.github.io/ksadk-python/assets/ksadk-web-ui-screenshot.png)

![KsADK 真实 Web UI 调试 GIF](https://kingsoftcloud.github.io/ksadk-python/assets/ksadk-local-debugging-demo.gif)

如果你的模型服务不是默认 OpenAI endpoint，再额外配置：

```bash
agentengine config set OPENAI_BASE_URL=https://api.example.com/v1
```

如果需要调用金山云 AgentEngine、Skill Service、知识库或长期记忆等线上能力，建议显式设置线上默认地域：

```bash
agentengine config set KSYUN_REGION=cn-beijing-6
```

## 架构

![KsADK Agent Runtime Platform 架构](https://kingsoftcloud.github.io/ksadk-python/assets/ksadk-runtime-architecture.png)

这张图展示的是公开运行时边界：业务 Agent 仍然由 ADK、LangGraph、LangChain 或 DeepAgents 编写；KsADK 在上层补齐统一 CLI、Web UI、OpenAI-Compatible API、Skill Runtime、Workspace、Sandbox、记忆、知识库和部署后端。

<details>
<summary>查看文本版架构</summary>

```text
Agent Code
  ADK / LangGraph / LangChain / DeepAgents
        |
        v
KsADK SDK
  runner adapters / config / toolsets
        |
        v
Unified Runtime
  CLI / Web UI / OpenAI-Compatible API
        |
        +-- Skill Runtime
        +-- Workspace Tools
        +-- Sandbox Runtime
        +-- Memory & Knowledge
        |
        v
AgentEngine
  Serverless / Hermes / OpenClaw Runtime
```

</details>

## 支持的框架

| 框架 | KsADK 负责什么 |
| --- | --- |
| Google ADK | 项目模板、Runner 适配、本地运行、Web UI 调试和部署入口。 |
| LangGraph | 图状态入口、工具调用、streaming、Skill Runtime 和 workspace toolsets。 |
| LangChain | Runnable/chain 适配、本地 OpenAI-Compatible API 和 tracing。 |
| DeepAgents | 项目入口、运行时包装、浏览器调试和部署制品。 |

## 能力对比

| 能力 | ADK | LangGraph | OpenAI Agents SDK | KsADK |
| --- | --- | --- | --- | --- |
| Agent 开发 | 支持 | 支持 | 支持 | 支持 |
| 浏览器调试 UI | 不内置 | 不内置 | 不内置 | 支持 |
| 统一 CLI | 不内置 | 不内置 | 不内置 | 支持 |
| OpenAI 兼容 API | 不内置 | 不内置 | 部分支持 | 支持 |
| 沙箱运行时 | 不内置 | 不内置 | 不内置 | 支持 |
| 部署工作流 | 不内置 | 不内置 | 不内置 | 支持 |
| 多运行时后端 | 不内置 | 不内置 | 不内置 | 支持 |

这张表只比较“项目自带的统一运行时平台能力”。KsADK 的设计目标不是替代这些框架，而是把它们放进同一套运行、调试、部署和观测体验里。

## 核心能力

- `agentengine init`：创建或导入 Agent 项目。
- `agentengine config`：管理 `.env` 和 `agentengine.yaml`。
- `agentengine run`：本地终端运行和交互调试。
- `agentengine web`：启动本地 Web UI，验证 streaming、附件、workspace、工具调用和会话。
- `/v1/responses` 与 `/v1/chat/completions`：提供 OpenAI-Compatible API。
- `ksadk.toolsets`：提供 Skill、Workspace、Platform、Sandbox 内置工具。
- Skill Runtime：发现、下载、校验、加载并隔离执行 Skill workflow。
- Sandbox Runtime：通过可配置后端隔离执行命令或代码。
- Hermes & OpenClaw：面向更完整 runtime 后端的部署和更新路径。

## 样例

样例仓库按场景组织，而不是只按技术框架分类：

- [KSADK Samples](https://github.com/kingsoftcloud/ksadk-samples)
- 知识助手（Knowledge Assistant）：知识库问答和 RAG。
- 工作流 Agent（Workflow Agent）：LangGraph + AgentEngine toolsets。
- 工具调用 Agent（Tool-Using Agent）：自定义工具调用。
- 记忆增强 Agent（Memory-aware Agent）：短期记忆和长期记忆接入。

每个公开 demo 都应包含中文 README、运行命令、环境变量说明、降级行为和验证问题。

## 部署

KsADK 支持本地优先的开发路径，也提供经过审核后可使用的部署入口：

```bash
agentengine build .
agentengine launch . --target serverless
agentengine dashboard open
```

Hermes 和 OpenClaw 更新已有实例时默认保留服务端已有 env、storage、network、memory 配置，只在显式传入对应 CLI 参数时覆盖，避免升级镜像时误改用户配置。

## 可观测

KsADK 原生面向 OpenTelemetry 设计。

你可以优先使用标准 OTLP 环境变量：

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.example.com
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20token
```

可对接：

- Langfuse
- Arize
- Datadog
- Grafana
- Phoenix

配置一次，到处观测。

## 0.6.4 重点

- 将公开定位从传统 Agent SDK 调整为 Agent Runtime Platform，首页补齐 Why KsADK、30 秒体验、架构图、对比表、Deployment、Observability 和 Community。
- 重构文档首页和 MkDocs 导航为 Getting Started / Build / Run / Deploy / Observe / Extend / Reference。
- 清理 README、CHANGELOG、文档和后续 PyPI 元数据中的环境特定表述，避免公开页面出现内部环境名或内部 header。
- 将公开定位和敏感词扫描纳入 `public-preflight`，防止后续回退。

## 0.6.3 重点

- Hosted UI 与最新 gateway / server 契约对齐，覆盖 `/hosted-ui/chat/`、分享链接、SSE 订阅和 native terminal 代理。
- LangGraph runner 在工具调用后即使没有文本流式 chunk，也会输出最终 answer，避免本地 Web UI 出现空 assistant message。
- Skill Service 增强环境化路由能力，支持通过环境变量配置服务地址、region 与必要请求头映射。
- OpenClaw / Hermes 更新已有实例时默认保留服务端已有 env、storage、network、memory 配置。
- `ksadk.toolsets`、Tool Gateway、Skill Runtime 与 Skill Service 相关文件纳入发布包，LangGraph demo 可在干净安装后绑定 AgentEngine 内置工具。

## 文档

- 文档：<https://kingsoftcloud.github.io/ksadk-python/>
- 中文文档：<https://kingsoftcloud.github.io/ksadk-python/zh/>
- English documentation：<https://kingsoftcloud.github.io/ksadk-python/en/>
- 命令行参考：<https://kingsoftcloud.github.io/ksadk-python/reference/cli/>
- OpenAI-Compatible API：<https://kingsoftcloud.github.io/ksadk-python/reference/openai-compatible-api/>

## 社区

- 仓库：<https://github.com/kingsoftcloud/ksadk-python>
- Wiki：<https://zread.ai/kingsoftcloud/ksadk-python>
- 示例仓库：<https://github.com/kingsoftcloud/ksadk-samples>
- Web UI 仓库：<https://github.com/kingsoftcloud/ksadk-web>
- PyPI：<https://pypi.org/project/ksadk/>
- 开源协议：Apache-2.0
