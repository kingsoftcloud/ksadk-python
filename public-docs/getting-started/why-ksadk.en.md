# Why KsADK

KsADK is not another agent framework. It is a unified runtime layer for agents
built with existing frameworks.

Most frameworks answer:

```text
How do I build an agent?
```

KsADK focuses on:

```text
How do I run, debug, expose, deploy, and observe agents consistently?
```

## One-line Positioning

Build agents once. Run them anywhere.

KsADK is the Agent Runtime Platform for AI agents.

Keep building business logic with Google ADK, LangGraph, LangChain, or
DeepAgents. Use KsADK for one CLI, local Web UI, OpenAI-Compatible API, Skill
Runtime, Workspace, Sandbox, deployment, and OpenTelemetry observability path.

## Why Not Just ADK

Google ADK focuses on agent modeling, tools, multi-agent collaboration,
Session/Memory, local runs, and Web debugging.

KsADK does not replace ADK. KsADK lets ADK agents share the same local runtime,
browser debugging UI, OpenAI-Compatible API, and deployment entrypoint as
LangGraph, LangChain, and DeepAgents projects.

KsADK is useful when:

- your team has both ADK and LangGraph projects.
- you want one Web UI to debug agents built with different frameworks.
- you need to expose local agents through OpenAI-Compatible APIs.
- you need Kingsoft Cloud AgentEngine, Skill, Workspace, Sandbox, or deployment
  integration.

## Why Not Just LangGraph

LangGraph is strong at graph-state orchestration, checkpointing, streaming,
human-in-the-loop workflows, and the LangChain ecosystem.

KsADK does not rewrite LangGraph execution. It adds a runtime platform layer
around LangGraph projects:

- `agentengine run` for local terminal interaction.
- `agentengine web` for browser debugging.
- `/v1/responses` and `/v1/chat/completions` local protocols.
- Skill Runtime, Workspace, and Sandbox toolsets.
- AgentEngine, Hermes, OpenClaw, and Serverless deployment entrypoints.

## Why Not Just OpenAI Agents SDK

OpenAI Agents SDK is native to the OpenAI Responses API and provides agent
orchestration, tool calling, handoffs, guardrails, and tracing.

KsADK is multi-framework and multi-runtime. It does not require your business
agent to be written with the OpenAI Agents SDK. It wraps ADK, LangGraph,
LangChain, and DeepAgents projects into one local runtime, debugging,
OpenAI-Compatible API, and deployment workflow.

## What KsADK Solves

| Problem | KsADK capability |
| --- | --- |
| Start a new agent project quickly | `agentengine init`, `agentengine config`, `agentengine run` |
| Debug streaming, attachments, and tool calls in a browser | `agentengine web` and the ksadk-web debugging UI |
| Use one invocation protocol across frameworks | `/v1/responses`, `/v1/chat/completions` |
| Add tools, workspace access, and isolated execution | Skill Runtime, Workspace tools, Sandbox tools |
| Deploy to remote runtimes | `agentengine build`, `agentengine launch`, Hermes/OpenClaw |
| Observe consistently | OpenTelemetry / OTLP tracing |

## When You May Not Need KsADK

If you only have a single-file script that directly calls a model API and you do
not need browser debugging, isolated tools, unified protocols, deployment, or
observability, the framework itself may be enough.

KsADK becomes useful when you need one way to manage multiple agent projects.
