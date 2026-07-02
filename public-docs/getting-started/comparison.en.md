# Ecosystem Positioning

This page is not a feature scorecard. ADK, LangGraph, OpenAI Agents SDK, VEADK,
and AgentRun each have mature capability boundaries.

KsADK's core positioning is to add a unified runtime platform layer above those
frameworks.

## Comparison Principle

To avoid misleading readers, this page compares public project focus and the
complementary layer KsADK adds. It avoids simplistic yes/no feature scoring.

| Project | Public project focus | KsADK's complementary layer |
| --- | --- | --- |
| Google ADK | Agent modeling, tools, multi-agent collaboration, Session/Memory, local runs, and Web debugging. | Run ADK agents alongside LangGraph, LangChain, and DeepAgents through one `agentengine` CLI, Web UI, local OpenAI-Compatible API, and deployment entrypoint. |
| LangGraph | Graph-state orchestration, streaming, checkpointing, human-in-the-loop workflows, and the LangChain ecosystem. | Add Skill Runtime, Workspace, Sandbox, Kingsoft Cloud AgentEngine, and deployment workflows around LangGraph projects. |
| OpenAI Agents SDK | OpenAI Responses API-native orchestration, tool calling, handoffs, guardrails, and tracing. | Expose non-OpenAI framework agents through local OpenAI-Compatible APIs, KsADK Web UI, and deployment workflows across multiple runtime backends. |
| VEADK | Agent development, A2UI/Frontend, AgentKit, VeFaaS, memory, knowledge base, built-in tools, and tracing for the Volcengine ecosystem. | Integrate Kingsoft Cloud AgentEngine, Skill, Workspace, Sandbox, Hermes/OpenClaw, and the open-source ksadk-web debugging experience. |
| AgentRun | Serverless Devs scaffolding and deployment, AgentRuntime lifecycle, OpenAI-compatible invocation, MCP/FunctionCall tools, Sandbox, knowledge base, and memory collection for Alibaba Cloud AgentRun Runtime. | Validate local multi-framework agents through one CLI, Web UI, tool, and sandbox path before connecting them to Kingsoft Cloud AgentEngine, Hermes/OpenClaw, and Skill Runtime. |

Terms such as A2UI/Frontend, VeFaaS, Serverless Devs, and AgentRuntime lifecycle
are kept intentionally so the comparison is grounded in public project
boundaries instead of flattening real capabilities into vague yes/no cells.

## How To Choose

| Your goal | Recommended path |
| --- | --- |
| Build one agent quickly with one framework | Start directly with ADK, LangGraph, LangChain, or OpenAI Agents SDK |
| Add local Web UI and OpenAI-Compatible APIs to an existing LangGraph/ADK project | Wrap the project with KsADK |
| Maintain agents built with multiple frameworks | Use KsADK to unify runtime, debugging, and deployment entrypoints |
| Use Kingsoft Cloud AgentEngine, Skill, Workspace, Sandbox, or Hermes/OpenClaw | Use the KsADK runtime and deployment workflow |

## What KsADK Does Not Do

- it does not force you to migrate to one agent framework.
- it does not reimplement every framework capability.
- it does not use inaccurate scorecards to downplay other projects.
- it does not rewrite model output by default; if you need Markdown shape repair,
  call `ksadk.markdown.repair_markdown(text, enabled=True)` in application code.

## Continue Reading

- [Why KsADK](why-ksadk.en.md)
- [Architecture](architecture.en.md)
- [Quick Start](quickstart.en.md)
