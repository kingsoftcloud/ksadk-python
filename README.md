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

## 0.8.3 运行时架构

KsADK 0.8.3 把“框架适配”收敛为稳定的运行时分层，同时保留各框架的原生执行语义：

- **可信内核**：统一并发、取消、恢复、状态一致性和运行时安全边界。
- **Harness 执行层**：负责装配、Activation、生命周期和共用能力注入；一次 Activation 只选择一个 Provider。
- **可插拔 Provider**：Codex、KsADK Harness、DSH/Cordis 与 Subagent 在同一 Harness 契约下运行，Provider 保留原生线程、checkpoint 与事件语义。
- **统一事件**：`RuntimeEvent(schema_version=2)` 是存储、回放、API、Studio 与托管界面的事件事实来源；v1 仅作为只读兼容投影。
- **受控插件化**：DSH Bundle/Profile 使用固定工具链、不可变来源摘要和失败回滚；Codex 官方插件仍由 Codex App Server 管理。
- **本地开发闭环**：Studio 覆盖创建、构建、调试、评测与本地 Scheduler Lite；配套 Web UI 固定为 `@kingsoftcloud/ksadk-web@0.3.4`。

从 [0.8.3 运行时架构](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/runtime-architecture/)、[AgentKit Local Studio](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/agentkit-local-studio/) 和[插件与自动化](https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/plugins-and-automations/)开始阅读。版本演进与发行状态见 [CHANGELOG](CHANGELOG.md) 和 PyPI 徽章。

### RuntimeEvent schema v2 契约

运行事件主路径固定为 canonical `RuntimeEvent(schema_version=2)`；能力描述为 `RuntimeEventVersions=[1,2]`、`RuntimeEventDefault=2`、`RuntimeEventV1ProjectionModes=["snapshot_only","identity_replace"]`、`RuntimeEventV1ProjectionDefault="snapshot_only"`。v1 仅作只读兼容投影。

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

<p align="center"><a href="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-runtime-architecture.png"><img alt="KsADK 总体技术架构" src="https://raw.githubusercontent.com/kingsoftcloud/ksadk-python/main/docs-site/public/assets/ksadk-runtime-architecture.png" width="860" /></a></p>

Agent Kernel 收口可信控制，Harness 管理装配与生命周期，可插拔 Provider 保留框架原生执行语义；RuntimeEvent v2 为 API、Studio 与托管界面提供统一事件事实链。

## 文档与样例

- 文档：<https://kingsoftcloud.github.io/ksadk-python/>
- 快速开始：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/quickstart/>
- 为什么需要 KsADK：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/why-ksadk/>
- 架构：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/architecture/>
- 生态定位对比：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/getting-started/comparison/>
- 可观测：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/observability-tracing/>
- 云端部署：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/cloud-deployment/>
- Hosted UI 与事件回放：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/framework/guides/hosted-ui-events/>
- 环境变量：<https://kingsoftcloud.github.io/ksadk-python/cn/docs/references/environment-variables/>
- 样例仓库：<https://github.com/kingsoftcloud/ksadk-samples>

## 相关项目

- KsADK 仓库：<https://github.com/kingsoftcloud/ksadk-python>
- Web UI 仓库：<https://github.com/kingsoftcloud/ksadk-web>
- PyPI：<https://pypi.org/project/ksadk/>

## 参与贡献

欢迎通过 issue、PR、样例和文档改进参与贡献。提交前建议运行：

```bash
make public-preflight
```

开源协议：Apache-2.0。
