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

<p align="center"><a href="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-runtime-platform-hero.png"><img alt="KsADK 真实 CLI 截图：agentengine -h" src="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-runtime-platform-hero-wide.png" width="860" /></a></p>

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

## 0.8.2 Agent Runtime V2 Phase 1

- Studio 现已覆盖本地创建、构建、调试以及云端部署、状态、详情、会话、更新、删除和版本回滚；也可以选择账号中由 CLI 部署的高代码 Agent。
- 云端请求由 Studio 本地服务使用 AK/SK 签名并经过 Server 准入，浏览器不持有云凭证；Gateway 不再绕过 Server 直连 Runtime。
- 普通前台对话使用真实 SSE 流；正文、思考、工具与审批可增量渲染。Goal 与 Plan 作为明确的执行控制，Background 只用于需要脱离前台连接的长任务。
- AgentKernelStore 默认允许 InMemory 或 SQLite；PostgreSQL 仅在需要跨 Pod 接管、恢复和高可用时启用。
- 配套 Web UI 固定为 `@kingsoftcloud/ksadk-web@0.3.2`。

完整操作见 [AgentKit Local Studio](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/agentkit-local-studio/)，详细变更见 [CHANGELOG](CHANGELOG.md)。

## 0.8.3 Agent Runtime V2 Phase 2（发布准备中）

Phase 2 正在把 KsADK 的可扩展能力收敛为受控插件体系，同时保持已发布 Agent 与历史 Bundle 的原路径可用：

- DSH Bundle/Profile 是唯一默认插件生态；`agentengine plugin` 使用固定版本的受管理 DSH/pnpm 工具链完成创建、校验、测试和打包，开发者无需检出 DeepSeek Harness 源码。KsADK 不再定义“原生 KsADK 插件”包格式。
- 本地目录或 `.tgz` 会先按 SHA-256 固化为不可变安装源；投影前发现来源漂移会 fail closed，升级失败会恢复旧 manifest、lock、状态与仍可执行的旧包。一个真实仓外 DSH AgentProvider 已通过同一 Profile 的安装、连续两轮、停用、失败升级回滚、重新启用和卸载 E2E。
- Codex 官方插件继续由 Codex App Server 管理，KsADK 不复制其实现或接管宿主权限。DSH Codex Bundle/child Provider、Claude Code 以及任意未经 conformance 的第三方 Provider 仍不属于已完成能力。
- Studio Scheduler Lite 已覆盖本地 once、interval、cron、时区、立即运行和历史，并同时进入全局自动化页与 Agent 详情页；云端 24×7 调度留在后续端云阶段。
- `ConversationSurface`、`ConversationInput`、`ConversationItem`、核心 Renderer 与 A2UI bridge 统一 Studio、Hosted UI 和自定义前端的输入输出边界。`ksadk-web@0.3.3` 的源码/浏览器门禁以及真实部署的新、历史 Agent 两轮会话已经通过；正式版仍须从公开 npm 制品重建并完成最终公开审计。

当前内容是未发布预览。能力边界、命令和兼容策略见[插件与自动化](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/plugins-and-automations/)和 [0.8.3 CHANGELOG 草案](CHANGELOG.md#083---unreleased)。

## 0.8.1 可观测性契约

- 远端 trace 统一使用标准 OTLP/HTTP：Langfuse 读取 `OTEL_EXPORTER_OTLP_*`，CloudMonitor 读取 `CLOUD_MONITOR_OTLP_*`；同一 span 在两端保持相同的 `trace_id` / `span_id`。
- 托管 Agent 通过 CLI 或控制台创建时默认开启可观测性并由平台注入双路配置；只有显式传入 `--no-observability` 或在控制台关闭才禁用。
- `LANGFUSE_USE_CALLBACK`、Langfuse SDK CallbackHandler/exporter 已移除。`CLOUD_MONITOR_APP_KEY` 只保留一个版本的过渡 fallback，新配置应通过 OTLP headers 提供 `Ksc-Appkey`。
- exporter 直接运行在 Agent 进程内，不会额外启动 OpenTelemetry Collector、sidecar、容器或 Pod。

迁移与环境变量示例见[可观测指南](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/observability-tracing/)和[环境变量参考](https://kingsoftcloud.github.io/ksadk-python/cn/docs/references/environment-variables/)。

## 0.8.1 RuntimeEvent schema v2 契约

- 运行事件主路径使用 canonical `RuntimeEvent(schema_version=2)`：runtime、协议投影、事件存储、回放与最终输出选择都以 v2 为唯一事实来源。
- v1 事件转为只读兼容投影，不再接受新的 v1 写入；未升级的下游消费者收到终端快照，已升级的消费者可显式选择 identity-aware 的 replace 语义。
- 能力描述：`RuntimeEventVersions=[1,2]`、`RuntimeEventDefault=2`、`RuntimeEventV1ProjectionModes=["snapshot_only","identity_replace"]`、`RuntimeEventV1ProjectionDefault="snapshot_only"`。
- 本地 Web UI、Studio 与 Hosted UI 必须使用与本次 Python 发布一致的 identity-aware 版本，才能按 item identity 正确归并流式与回放输出。

<p align="center"><a href="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-web-ui-screenshot.png"><img alt="KsADK 真实 Web UI 调试截图" src="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-web-ui-screenshot.png" width="860" /></a></p>

<p align="center"><a href="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-local-debugging-demo.gif"><img alt="KsADK 真实本地 Web UI 演示" src="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-local-debugging-demo.gif" width="860" /></a></p>

## 为什么需要 KsADK

大多数 Agent 框架解决“如何开发 Agent”。KsADK 解决“如何运行、调试、部署和观测 Agent”。

- 本地开发：`agentengine init`、`agentengine run`、`agentengine web`。
- 统一调试：浏览器 Web UI、streaming、附件、workspace 文件、工具调用和会话。
- 统一协议：本地 `/v1/responses` 与 `/v1/chat/completions`。
- 工具边界：Skill Runtime、Workspace、Sandbox、Memory、Knowledge。
- 工程链路：打包、部署、OpenClaw / Hermes 运行时、OpenTelemetry 可观测。

## 架构

<p align="center"><a href="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-runtime-architecture.png"><img alt="KsADK 智能体运行时平台架构" src="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-runtime-architecture.png" width="860" /></a></p>

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
