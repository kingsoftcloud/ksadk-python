# ksadk

[简体中文](README.md) | [English](README.en.md)

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/kingsoftcloud/ksadk-python)

金山云 Agent Development Kit。`ksadk` 提供 Python SDK 和命令行，
安装后可使用 `agentengine` 或等价的 `ksadk` 命令创建、运行、调试、
打包和部署 AgentEngine 智能体项目。它面向
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

以下示例使用 `agentengine`；所有命令也可以把 `agentengine` 替换为 `ksadk`。

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
- Skill Runtime：Skill Space 发现、zip 下载、`sha256` 校验、安全解压、instruction 加载，以及 `local_process` 或 E2B sandbox workflow 执行
- AgentEngine 内置工具：skill 发现/加载、workspace 文件操作、component status、sandbox status 和 sandbox direct code/command execution
- Sandbox Runtime：通用沙箱抽象与 E2B 兼容后端

## 0.6.2 重点

- Skill Runtime 支持 Skill Space 远端发现、按需下载、`sha256` 校验、安全解压、`SKILL.md` instruction 加载，以及 `local_process` / E2B sandbox backend workflow 执行。
- `ksadk.toolsets` 提供 Skill、Workspace、Platform、Sandbox 内置工具；推荐绑定 `get_agentengine_tools(include=["focused", "agentengine_tool_dispatcher"])`，把低频或高风险工具放进 dispatcher 按需 `list` / `describe` / `call`。
- Tool Gateway 为 workspace 写入/删除、Skill Runtime 执行、sandbox command/code 等中高风险操作提供统一 `approval_required` envelope。
- Workspace tools 新增 exact snippet edit 与 lightweight lint；Sandbox tools 新增 direct `run_command` / `run_code`，且只通过 configured isolated sandbox backend 执行。
- `setup_tracing()` 优先识别标准 `OTEL_EXPORTER_OTLP_*` HTTP traces 配置，Langfuse 环境变量仍保持兼容。
- 环境变量 registry 和公开文档覆盖 OTLP traces、AICP endpoint mode、Skill Service endpoint/scheme、Sandbox Runtime、Skill Runtime 和 Tool Gateway settings。

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
- 示例仓库：<https://github.com/kingsoftcloud/ksadk-samples>
- Web UI 仓库：<https://github.com/kingsoftcloud/ksadk-web>
- PyPI：<https://pypi.org/project/ksadk/>
- 开源协议：Apache-2.0

## 说明

- Skill 注册、CRUD 和版本治理属于 Skill Service；`ksadk` 在运行时消费 Skill Center。
- Sandbox 模板和实例生命周期属于 Sandbox Service；`ksadk` 使用配置的沙箱后端执行运行时工作流。
- E2B 兼容沙箱后端使用原生 `E2B_API_URL` 和 `E2B_API_KEY` 环境变量。
