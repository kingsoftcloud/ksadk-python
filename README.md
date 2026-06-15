<h1 align="center">KsADK</h1>

<p align="center"><strong>一次构建 Agent，到处运行。Build agents once. Run them anywhere.</strong></p>

<p align="center">
  KsADK 是面向 AI Agent 的运行时平台（Agent Runtime Platform）。
  继续使用 Google ADK、LangGraph、LangChain 或 DeepAgents 编写业务 Agent，再用统一 CLI、Web UI、OpenAI-Compatible API、工具运行时、沙箱、部署和可观测链路把它跑起来。
</p>

<p align="center"><a href="README.md">简体中文（默认）</a> · <a href="README.en.md">English</a></p>

<p align="center">
  <a href="https://kingsoftcloud.github.io/ksadk-python/"><img alt="Docs" src="https://img.shields.io/badge/Docs-ksadk--python-2f6fdf?style=flat" /></a>
  <a href="https://pypi.org/project/ksadk/"><img alt="PyPI" src="https://img.shields.io/pypi/v/ksadk?style=flat&color=2f6fdf" /></a>
  <a href="https://zread.ai/kingsoftcloud/ksadk-python"><img alt="Ask Zread" src="https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff" /></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-Apache--2.0-blue?style=flat" /></a>
</p>

<p align="center"><a href="public-docs/assets/ksadk-runtime-platform-hero.png"><img alt="KsADK 真实 CLI 截图：agentengine -h" src="public-docs/assets/ksadk-runtime-platform-hero-wide.png" width="860" /></a></p>

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

如果需要调用金山云 AgentEngine、Skill Service、知识库或长期记忆等线上能力，建议显式设置线上默认地域：

```bash
agentengine config set KSYUN_REGION=cn-beijing-6
```

启动本地调试 Web UI：

```bash
agentengine web . --no-open
```

<p align="center"><img alt="KsADK 真实 Web UI 调试截图" src="public-docs/assets/ksadk-web-ui-screenshot.png" width="860" /></p>

<p align="center"><img alt="KsADK 真实本地 Web UI 演示" src="public-docs/assets/ksadk-local-debugging-demo.gif" width="860" /></p>

## 为什么需要 KsADK

大多数 Agent 框架解决“如何开发 Agent”。KsADK 解决“如何运行、调试、部署和观测 Agent”。

- 本地开发：`agentengine init`、`agentengine run`、`agentengine web`。
- 统一调试：浏览器 Web UI、streaming、附件、workspace 文件、工具调用和会话。
- 统一协议：本地 `/v1/responses` 与 `/v1/chat/completions`。
- 工具边界：Skill Runtime、Workspace、Sandbox、Memory、Knowledge。
- 工程链路：打包、部署、OpenTelemetry 可观测。

## 架构

<p align="center"><img alt="KsADK Agent Runtime Platform 架构" src="public-docs/assets/ksadk-runtime-architecture.png" width="860" /></p>

## 文档与样例

- 文档：<https://kingsoftcloud.github.io/ksadk-python/>
- 快速开始：<https://kingsoftcloud.github.io/ksadk-python/getting-started/quickstart/>
- 为什么需要 KsADK：<https://kingsoftcloud.github.io/ksadk-python/getting-started/why-ksadk/>
- 架构：<https://kingsoftcloud.github.io/ksadk-python/getting-started/architecture/>
- 生态定位对比：<https://kingsoftcloud.github.io/ksadk-python/getting-started/comparison/>
- 可观测：<https://kingsoftcloud.github.io/ksadk-python/guides/observability-tracing/>
- 样例仓库：<https://github.com/kingsoftcloud/ksadk-samples>

## 相关项目

- KsADK 仓库：<https://github.com/kingsoftcloud/ksadk-python>
- Web UI 仓库：<https://github.com/kingsoftcloud/ksadk-web>
- Wiki：<https://zread.ai/kingsoftcloud/ksadk-python>
- PyPI：<https://pypi.org/project/ksadk/>

## 参与贡献

欢迎通过 issue、PR、样例和文档改进参与贡献。提交前建议运行：

```bash
make public-preflight
```

开源协议：Apache-2.0。
