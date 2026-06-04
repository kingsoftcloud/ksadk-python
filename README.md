# ksadk

[简体中文](README.md) | [English](README.en.md)

金山云 Agent Development Kit。`ksadk` 提供 Python SDK 和 `agentengine`
命令行，用于创建、运行、调试、打包和部署 AgentEngine 智能体项目。它面向
本地开发、Serverless 运行时、Google ADK、LangChain/LangGraph、
DeepAgents、Hermes、OpenClaw、MCP 和 Skill Runtime 等场景。

当前版本：`0.6.2`。

## 安装

```bash
pip install -U ksadk
```

按需安装可选运行时依赖：

```bash
pip install -U "ksadk[adk]"
pip install -U "ksadk[langgraph]"
pip install -U "ksadk[deepagents]"
pip install -U "ksadk[skills]"
pip install -U "ksadk[all]"
```

## 快速开始

创建并运行一个本地 Agent：

```bash
agentengine init my-agent -f langgraph
cd my-agent
agentengine config
agentengine run -i
```

打开本地 Web UI：

```bash
agentengine web . --no-open
```

如需走部署形态，先使用 dry-run 或内部审核流程确认配置：

```bash
agentengine launch . --target serverless
```

## 包含能力

- 本地开发命令：`init`、`config`、`run`、`web`
- 构建与部署命令：`build`、`deploy`、`launch`
- 远程操作：`agent invoke`、`files`、`dashboard`
- 运行时集成：ADK、LangChain、LangGraph、DeepAgents、MCP
- 托管运行时资产：Hermes 和 OpenClaw
- Skill Runtime 预览：Skill Center 发现、zip 下载、`sha256` 校验、安全解压、本地执行和通过 `ksadk[skills]` 启用的沙箱执行
- Sandbox Runtime 预览：通用沙箱抽象与 E2B 兼容后端

## 0.6.2 重点

- `setup_tracing()` 优先识别标准 `OTEL_EXPORTER_OTLP_*` HTTP traces 配置，便于把链路发送到 Langfuse 或任意 OTLP Collector。
- Langfuse 环境变量仍保持兼容；当通用 OTLP 已配置时，自动模式不会重复启用 Langfuse 直连 exporter。
- tracing 文档补充 span event 与子 span 的后端可见性差异，以及用 `score.*` attributes 表达评估分数的推荐方式。
- Skill Runtime 保持公共 Skill Space、allowlist、E2B/Sandbox backend 等公开运行时能力。
- 继续保留 0.6.1 中的 Responses 输入语义、流式会话恢复、本地 sqlite session 和 workspace 预览体验优化。

## 文档

公开文档托管在 GitHub Pages，并使用 MkDocs Material 与双语 i18n 方案：

- [中文文档](https://kingsoftcloud.github.io/ksadk-python/zh/)
- [English documentation](https://kingsoftcloud.github.io/ksadk-python/en/)
- [快速开始](https://kingsoftcloud.github.io/ksadk-python/getting-started/quickstart/)
- [配置项](https://kingsoftcloud.github.io/ksadk-python/getting-started/configuration/)
- [命令行参考](https://kingsoftcloud.github.io/ksadk-python/reference/cli/)
- [OpenAI 兼容 API](https://kingsoftcloud.github.io/ksadk-python/reference/openai-compatible-api/)
- [贡献指南](https://github.com/kingsoftcloud/ksadk-python/blob/main/CONTRIBUTING.md)
- [安全策略](https://github.com/kingsoftcloud/ksadk-python/blob/main/SECURITY.md)

## 项目链接

- 文档：<https://kingsoftcloud.github.io/ksadk-python/>
- 仓库：<https://github.com/kingsoftcloud/ksadk-python>
- Web UI 仓库：<https://github.com/kingsoftcloud/ksadk-web>
- PyPI：<https://pypi.org/project/ksadk/>
- 开源协议：Apache-2.0

## 说明

- Skill 注册、CRUD 和版本治理属于 Skill Service；`ksadk` 在运行时消费 Skill Center。
- Sandbox 模板和实例生命周期属于 Sandbox Service；`ksadk` 使用配置的沙箱后端执行运行时工作流。
- E2B 兼容沙箱后端使用原生 `E2B_API_URL` 和 `E2B_API_KEY` 环境变量。
