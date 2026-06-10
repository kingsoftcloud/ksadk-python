# KsADK

[简体中文（默认）](README.md) | [English](README.en.md)

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/kingsoftcloud/ksadk-python)

一次构建 Agent，到处运行。

KsADK 是面向 AI Agent 的运行时平台（Agent Runtime Platform）。你可以继续使用 Google ADK、LangGraph、LangChain 或 DeepAgents 编写业务 Agent，再用 KsADK 获得统一的本地运行、浏览器调试、OpenAI-Compatible API、Skill Runtime、Workspace、Sandbox、部署和可观测体验。

本仓库默认 README 使用简体中文；英文内容维护在 [README.en.md](README.en.md)。

![KsADK 真实 CLI 截图：agentengine -h](public-docs/assets/ksadk-runtime-platform-hero.png)

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

启动真实本地 Web UI 演示：

```bash
agentengine web . --no-open
```

![KsADK 真实 Web UI 调试截图](public-docs/assets/ksadk-web-ui-screenshot.png)

![KsADK 真实本地 Web UI 演示 GIF](public-docs/assets/ksadk-local-debugging-demo.gif)

常用配置：

```bash
# 非默认 OpenAI endpoint 时再配置
agentengine config set OPENAI_BASE_URL=https://api.example.com/v1

# 调用金山云 AgentEngine、Skill Service、知识库或长期记忆等线上能力时建议显式配置
agentengine config set KSYUN_REGION=cn-beijing-6
```

## 为什么需要 KsADK

大多数 Agent 框架解决“如何开发 Agent”。KsADK 解决“如何运行、调试、部署和观测 Agent”。

- 统一 CLI：`agentengine init`、`agentengine run`、`agentengine web`。
- 统一调试：浏览器 Web UI、streaming、附件、workspace 文件、工具调用和会话。
- 统一协议：本地 `/v1/responses` 与 `/v1/chat/completions`。
- 统一工具边界：Skill Runtime、Workspace、Sandbox、Memory、Knowledge。
- 统一工程链路：打包、部署、OpenTelemetry 可观测。

## 架构一览

![KsADK Agent Runtime Platform 架构](public-docs/assets/ksadk-runtime-architecture.png)

详细说明见 [为什么需要 KsADK](https://kingsoftcloud.github.io/ksadk-python/getting-started/why-ksadk/)、[架构](https://kingsoftcloud.github.io/ksadk-python/getting-started/architecture/) 和 [生态定位对比](https://kingsoftcloud.github.io/ksadk-python/getting-started/comparison/)。

生态定位页会按事实说明 KsADK 与 Google ADK、LangGraph、OpenAI Agents SDK、VEADK、AgentRun 的互补关系，不使用误导性的能力打分榜。

## 核心能力

| 能力 | 入口 |
| --- | --- |
| 本地开发 | `agentengine init`、`agentengine config`、`agentengine run` |
| 浏览器调试界面 | `agentengine web` |
| OpenAI-Compatible API | `/v1/responses`、`/v1/chat/completions` |
| 多框架运行 | ADK / LangGraph / LangChain / DeepAgents Runner |
| 工具与隔离执行 | `ksadk.toolsets`、Skill Runtime、Workspace、Sandbox |
| 可选 Markdown 修复 | `ksadk.markdown.repair_markdown` 按需修复；runtime 默认不改写模型原文 |
| 部署与可观测 | Serverless / Hermes / OpenClaw、OpenTelemetry |

## 样例

- [KSADK Samples](https://github.com/kingsoftcloud/ksadk-samples)
- Knowledge Assistant：知识库问答和 RAG。
- Workflow Agent：LangGraph + AgentEngine toolsets。
- Tool-Using Agent：自定义工具调用。
- Memory-aware Agent：短期记忆和长期记忆接入。

## 文档与社区

- 文档：<https://kingsoftcloud.github.io/ksadk-python/>
- 命令行参考：<https://kingsoftcloud.github.io/ksadk-python/reference/cli/>
- 环境变量：<https://kingsoftcloud.github.io/ksadk-python/reference/environment-variables/>
- 更新日志：[CHANGELOG.md](CHANGELOG.md)
- GitHub Releases：<https://github.com/kingsoftcloud/ksadk-python/releases>
- Wiki：<https://zread.ai/kingsoftcloud/ksadk-python>
- Web UI 仓库：<https://github.com/kingsoftcloud/ksadk-web>
- PyPI：<https://pypi.org/project/ksadk/>

## 参与贡献

欢迎通过 issue、PR、样例和文档改进参与贡献。提交前建议先运行：

```bash
make public-preflight
```

开源协议：Apache-2.0。
