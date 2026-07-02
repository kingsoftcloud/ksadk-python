# 为什么需要 KsADK

KsADK 的定位不是再造一个 Agent 框架，而是给已有 Agent 框架补齐统一运行时。

大多数框架回答的是：

```text
如何编写 Agent？
```

KsADK 更关注：

```text
如何把 Agent 跑起来、调起来、接出去、部署出去，并持续观测？
```

## 一句话定位

一次构建 Agent，到处运行。

KsADK 是面向 AI Agent 的运行时平台（Agent Runtime Platform）。

你可以继续使用 Google ADK、LangGraph、LangChain 或 DeepAgents 编写业务逻辑，再用
KsADK 获得统一的 CLI、本地 Web UI、OpenAI-Compatible API、Skill Runtime、
Workspace、Sandbox、部署和 OpenTelemetry 观测能力。

## 为什么不是 ADK

Google ADK 主要解决 Agent 建模、工具、多 Agent 协作、Session/Memory、本地运行与
Web 调试。

ADK 解决 Agent 开发；KsADK 解决 Agent 运行。

KsADK 不替代 ADK。KsADK 的目标是让 ADK Agent 可以和 LangGraph、LangChain、
DeepAgents 项目共用同一套本地运行、浏览器调试、OpenAI-Compatible API 和部署入口。

适合使用 KsADK 的场景：

- 团队里同时有 ADK 和 LangGraph 项目。
- 需要用同一套 Web UI 调试不同框架的 Agent。
- 需要把本地 Agent 暴露成 OpenAI-Compatible API。
- 需要接入金山云 AgentEngine、Skill、Workspace、Sandbox 或部署链路。

## 为什么不是 LangGraph

LangGraph 擅长图状态编排、checkpoint、streaming、人机协作和 LangChain 生态。

KsADK 不重写 LangGraph 的图执行能力。KsADK 在 LangGraph 项目之外补上运行时平台层：

- `agentengine run` 本地交互。
- `agentengine web` 浏览器调试。
- `/v1/responses` 和 `/v1/chat/completions` 本地协议。
- Skill Runtime、Workspace 和 Sandbox toolsets。
- AgentEngine、Hermes、OpenClaw 和 Serverless 部署入口。

## 为什么不是 OpenAI Agents SDK

OpenAI Agents SDK 面向 OpenAI Responses API，提供 Agent 编排、工具调用、handoff、
guardrails 和 tracing。

KsADK 面向多框架和多运行后端。它不会要求你的业务 Agent 一定采用 OpenAI Agents SDK
编写，而是把 ADK、LangGraph、LangChain 和 DeepAgents 项目统一包装到本地运行、调试、
OpenAI-Compatible API 和部署链路里。

## KsADK 解决什么问题

| 问题 | KsADK 提供的能力 |
| --- | --- |
| 新 Agent 项目如何快速跑起来 | `agentengine init`、`agentengine config`、`agentengine run` |
| 如何用浏览器调试 streaming、附件和工具调用 | `agentengine web` 和 ksadk-web 调试界面 |
| 如何让不同框架使用同一套调用协议 | `/v1/responses`、`/v1/chat/completions` |
| 如何给 Agent 接入工具、workspace 和隔离执行 | Skill Runtime、Workspace tools、Sandbox tools |
| 如何部署到远端运行时 | `agentengine build`、`agentengine launch`、Hermes/OpenClaw |
| 如何统一观测 | OpenTelemetry / OTLP tracing |

## 什么时候不需要 KsADK

如果你只是写一个单文件脚本，直接调用模型 API，没有浏览器调试、工具隔离、统一协议、
部署和观测需求，直接使用框架本身就够了。

当你开始需要“同一套方式管理多个 Agent 项目”时，KsADK 的价值才会变明显。
