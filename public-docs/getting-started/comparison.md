# 生态定位对比

这页不是能力打分榜。ADK、LangGraph、OpenAI Agents SDK、VEADK 和 AgentRun 都有自己成熟
的能力边界。

KsADK 的核心定位是：在这些框架之上补一层统一运行时平台。

## 对比原则

为了避免误导，这里只比较公开项目的主要侧重点和 KsADK 的互补层，不用“谁有谁没有”的
简单打分表。

| 项目 | 公开项目侧重点 | KsADK 更关注的互补层 |
| --- | --- | --- |
| Google ADK | Agent 建模、工具、多 Agent 协作、Session/Memory、本地运行与 Web 调试。 | 让 ADK Agent 和 LangGraph、LangChain、DeepAgents 共用同一套 `agentengine` CLI、Web UI、本地 OpenAI-Compatible API 与部署入口。 |
| LangGraph | 图状态编排、streaming、checkpoint、人机协作和 LangChain 生态。 | 为 LangGraph 项目补齐 Skill Runtime、Workspace、Sandbox、金山云 AgentEngine 和部署链路。 |
| OpenAI Agents SDK | 面向 OpenAI Responses API 的 Agent 编排、工具调用、handoff、guardrails 和 tracing。 | 面向多框架和多运行后端，把非 OpenAI 框架 Agent 也暴露为本地 OpenAI-Compatible API，并接入 KsADK Web UI 与部署工作流。 |
| VEADK | 面向火山引擎生态的 Agent 构建、A2UI/Frontend、AgentKit、VeFaaS、记忆、知识库、内置工具和 tracing。 | 面向金山云生态整合 AgentEngine、Skill、Workspace、Sandbox、Hermes/OpenClaw 和 ksadk-web 开源调试体验。 |
| AgentRun | 面向阿里云 AgentRun Runtime 的 Serverless Devs 脚手架与部署、AgentRuntime 生命周期、OpenAI-compatible 调用、MCP/FunctionCall 工具、Sandbox、知识库和记忆集合。 | 让本地多框架 Agent 先通过统一 CLI、Web UI、工具与沙箱链路跑通，再接入金山云 AgentEngine、Hermes/OpenClaw 和 Skill Runtime。 |

这里保留 A2UI/Frontend、VeFaaS、Serverless Devs、AgentRuntime 生命周期等公开项目术语，
是为了让对比基于事实边界，而不是用模糊的“支持/不支持”压扁不同项目的真实能力。

## 怎么选择

| 你的目标 | 推荐路径 |
| --- | --- |
| 只想用一个框架快速写 Agent | 直接从 ADK、LangGraph、LangChain 或 OpenAI Agents SDK 开始 |
| 已经有 LangGraph/ADK 项目，需要本地 Web UI 和 OpenAI-Compatible API | 在项目外层接入 KsADK |
| 团队同时维护多个框架的 Agent | 用 KsADK 统一运行、调试和部署入口 |
| 需要金山云 AgentEngine、Skill、Workspace、Sandbox 或 Hermes/OpenClaw | 使用 KsADK 的 runtime 和部署链路 |

## KsADK 不做什么

- 不强迫你迁移到某一个 Agent 框架。
- 不把所有框架能力重新实现一遍。
- 不用不准确的功能打分表贬低其他项目。
- 不默认改写模型输出；如需 Markdown 形态修复，可在业务侧显式调用
  `ksadk.markdown.repair_markdown(text, enabled=True)`。

## 继续阅读

- [为什么需要 KsADK](why-ksadk.md)
- [架构](architecture.md)
- [快速开始](quickstart.md)
