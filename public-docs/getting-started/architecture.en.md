# Architecture

KsADK's public architecture is an Agent Runtime Platform layer. You keep building
business agents with your chosen framework, while KsADK unifies runtime,
debugging, protocols, tools, sandboxing, deployment, and observability.

![KsADK Agent Runtime Platform architecture](../assets/ksadk-runtime-architecture.png)

## Overview

```text
ADK / LangGraph / LangChain / DeepAgents
                    │
                    ▼
                  KsADK
                    │
    ┌───────────────┼────────────────┐
    ▼               ▼                ▼
 Skill Runtime   Workspace        Sandbox
    │               │                │
    └───────────────┼────────────────┘
                    ▼
              AgentEngine
                    │
                    ▼
          Hermes / OpenClaw Runtime
```

## Main Boundaries

| Layer | Responsibility |
| --- | --- |
| Agent frameworks | business orchestration, state, tool calls, and model interaction |
| KsADK CLI | project creation, config loading, local runs, Web UI startup, packaging |
| Runner | adapt ADK, LangGraph, LangChain, and DeepAgents to one invocation contract |
| Local server | expose `/v1/responses`, `/v1/chat/completions`, and local Web UI APIs |
| Toolsets | provide Skill, Workspace, Platform, and Sandbox tool entrypoints |
| AgentEngine / Hermes / OpenClaw | remote runtime, deployment, and fuller backend execution |
| OpenTelemetry | standard tracing output for external observability systems |

## Local Runtime Path

When you run `agentengine run` or `agentengine web`:

1. the CLI resolves the project directory, `.env`, and `agentengine.yaml`.
2. framework detection identifies ADK, LangGraph, LangChain, or DeepAgents.
3. the runner factory creates the matching runner.
4. the runner loads the user agent.
5. the terminal, Web UI, or OpenAI-Compatible API invokes the runner.
6. sessions, attachments, workspace files, tool calls, and tracing use the KsADK
   runtime path.

## Why This Architecture Matters

The boundary lets teams keep their preferred agent frameworks while sharing:

- one local command surface.
- one browser debugging experience.
- one OpenAI-Compatible API surface.
- one Skill / Workspace / Sandbox tool model.
- one deployment and observability path.

For lower-level implementation details, see [Runtime Architecture](../guides/runtime-architecture.en.md).
