<h1 align="center">Kingsoft Cloud Agent Development Kit</h1>

<p align="center"><strong>金山云智能体开发套件</strong></p>

<p align="center">
  构建、部署、调试、观测企业级 AI 智能体的一站式云原生框架。
  兼容 Google ADK、LangGraph、LangChain 与 DeepAgents，并支持一键拉起 OpenClaw 和 Hermes 运行时。
</p>

<p align="center"><a href="README.md">简体中文（默认）</a> · <a href="README.en.md">English</a></p>

<p align="center">
  <a href="https://kingsoftcloud.github.io/ksadk-python/"><img alt="Docs" src="https://img.shields.io/badge/Docs-ksadk--python-2f6fdf?style=flat" /></a>
  <a href="https://pypi.org/project/ksadk/"><img alt="PyPI" src="https://img.shields.io/pypi/v/ksadk?style=flat&color=2f6fdf" /></a>
  <a href="https://zread.ai/kingsoftcloud/ksadk-python"><img alt="Ask Zread" src="https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff" /></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-Apache--2.0-blue?style=flat" /></a>
</p>

<p align="center"><a href="docs-site/public/assets/ksadk-runtime-platform-hero.png"><img alt="KsADK 真实 CLI 截图：agentengine -h" src="docs-site/public/assets/ksadk-runtime-platform-hero-wide.png" width="860" /></a></p>

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

启动本地调试 Web UI：

```bash
agentengine web . --no-open
```

<p align="center"><img alt="KsADK 真实 Web UI 调试截图" src="docs-site/public/assets/ksadk-web-ui-screenshot.png" width="860" /></p>

<p align="center"><img alt="KsADK 真实本地 Web UI 演示" src="docs-site/public/assets/ksadk-local-debugging-demo.gif" width="860" /></p>

## 0.8.0 评审候选

`0.8.0` 是正在评审的候选分支，不是已发布的 PyPI/npm 版本。它把 RuntimeEvent、AG-UI/A2UI、A2A、Harness 和 CodexRuntime 统一到同一运行时边界，同时保持 `/v1/responses` 和 `/v1/chat/completions` 兼容入口可用。

- **Hosted UI**：AG-UI/A2UI 通过能力协商启用；不能协商时继续使用既有 Responses transport。
- **事件诊断**：`ksadk replay <session-id>` 只读回放新 RuntimeEvent 历史，可用 `--after-seq-id` / `--before-seq-id` 缩小排查窗口，不会再次执行模型、工具或审批副作用。
- **托管 A2A（实验性）**：AgentEngine 通过 Gateway 装配可信入站和 Space-scoped 出站 client。首期 external Agent 仅支持 `external_public`；它受 `Network.EnablePublicAccess` 控制并使用受限 NAT transport，`external_vpc` 尚不可用。
- **框架迁移**：新接入优先使用 LangGraph、Google ADK 或 `RuntimeAdapter`。旧 LangChain 连续性 / HITL 路径不属于 `0.8` 兼容性承诺。

详见[Hosted UI 与事件回放指南](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/hosted-ui-events/)、[托管 A2A Runtime 指南](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/a2a-runtime/)和 [0.8.0 更新日志](CHANGELOG.md)。

## 为什么需要 KsADK

大多数 Agent 框架解决“如何开发 Agent”。KsADK 解决“如何运行、调试、部署和观测 Agent”。

- 本地开发：`agentengine init`、`agentengine run`、`agentengine web`。
- 统一调试：浏览器 Web UI、streaming、附件、workspace 文件、工具调用和会话。
- 统一协议：本地 `/v1/responses` 与 `/v1/chat/completions`。
- 工具边界：Skill Runtime、Workspace、Sandbox、Memory、Knowledge。
- 工程链路：打包、部署、OpenClaw / Hermes 运行时、OpenTelemetry 可观测。

## 架构

<p align="center"><img alt="KsADK 智能体运行时平台架构" src="docs-site/public/assets/ksadk-runtime-architecture.png" width="860" /></p>

## 文档与样例

- 文档：<https://kingsoftcloud.github.io/ksadk-python/>
- 快速开始：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/quickstart/>
- 为什么需要 KsADK：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/why-ksadk/>
- 架构：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/architecture/>
- 生态定位对比：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/comparison/>
- 可观测：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/observability-tracing/>
- 云端部署：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/cloud-deployment/>
- Hosted UI 与事件回放：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/hosted-ui-events/>
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
