# KsADK

Build agents once. Run them anywhere.

KsADK is the Agent Runtime Platform for AI agents.

Build with Google ADK, LangGraph, LangChain, or DeepAgents. Run, debug, expose, observe, and deploy those agents through one unified runtime experience.

![KsADK local debugging demo](assets/ksadk-local-debugging-demo.gif)

=== "Install"

    ```bash
    pip install -U "ksadk[all]"
    ```

=== "Create"

    ```bash
    agentengine init demo-agent -f langgraph
    cd demo-agent
    agentengine config set OPENAI_API_KEY=your-api-key OPENAI_MODEL_NAME=gpt-4o-mini
    ```

=== "Run"

    ```bash
    agentengine run -i
    agentengine web . --no-open
    ```

## Why KsADK

Most agent frameworks solve agent development.

KsADK solves agent runtime.

KsADK does not replace your agent framework. It provides a unified platform layer for development, debugging, runtime, sandbox, deployment, and observability:

- Development: one CLI for project creation, configuration, and local runs.
- Debugging: browser UI, sessions, attachments, workspace files, and streaming.
- Runtime: framework runners, OpenAI-Compatible APIs, and consistent invocation.
- Sandbox: Skill Runtime, Workspace, and isolated sandbox backend boundaries.
- Deployment: Serverless, Hermes, OpenClaw, and remote AgentEngine entrypoints.
- Observability: OpenTelemetry-first tracing for multiple backends.

## 30 Seconds Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U "ksadk[all]"

agentengine init demo-agent -f langgraph
cd demo-agent
agentengine config set OPENAI_API_KEY=your-api-key OPENAI_MODEL_NAME=gpt-4o-mini
agentengine run -i
```

Open the local Web UI:

```bash
agentengine web . --no-open
```

If your model provider is not the default OpenAI endpoint, also set:

```bash
agentengine config set OPENAI_BASE_URL=https://api.example.com/v1
```

If you need Kingsoft Cloud AgentEngine, Skill Service, knowledge base, or long-term memory services, set the default public cloud region explicitly:

```bash
agentengine config set KSYUN_REGION=cn-beijing-6
```

## Architecture

![KsADK Agent Runtime Platform architecture](assets/ksadk-runtime-architecture.png)

This diagram shows the public runtime boundary: keep building business agents with ADK, LangGraph, LangChain, or DeepAgents, then use KsADK for one CLI, browser Web UI, OpenAI-Compatible APIs, Skill Runtime, Workspace, Sandbox, memory, knowledge, and deployment backends.

## Supported Frameworks

| Framework | What KsADK adds |
| --- | --- |
| Google ADK | Templates, runner adapter, local runtime, Web UI debugging, and deployment entrypoints. |
| LangGraph | Graph-state entrypoint, tool calling, streaming, Skill Runtime, and workspace toolsets. |
| LangChain | Runnable/chain adaptation, local OpenAI-Compatible APIs, and tracing. |
| DeepAgents | Project entrypoint, runtime wrapping, browser debugging, and deployment artifacts. |

## Comparison

| Capability | ADK | LangGraph | OpenAI Agents SDK | KsADK |
| --- | --- | --- | --- | --- |
| Agent Development | Yes | Yes | Yes | Yes |
| Browser Debugging UI | No | No | No | Yes |
| Unified CLI | No | No | No | Yes |
| OpenAI Compatible API | No | No | Partial | Yes |
| Sandbox Runtime | No | No | No | Yes |
| Deployment Workflow | No | No | No | Yes |
| Multi Runtime Backend | No | No | No | Yes |

This table compares unified runtime-platform capabilities provided by the project itself. KsADK complements agent frameworks instead of replacing them.

## Core Capabilities

| Capability | Common entrypoints |
| --- | --- |
| Local Development | `agentengine init`, `agentengine config`, `agentengine run` |
| Browser Debugging UI | `agentengine web` |
| OpenAI-Compatible API | `/v1/responses`, `/v1/chat/completions` |
| Unified Runtime | ADK / LangGraph / LangChain / DeepAgents runners |
| Sandbox Execution | Skill Runtime, Workspace tools, Sandbox tools |
| Serverless Deployment | `agentengine build`, `agentengine launch` |
| Hermes & OpenClaw Runtime | `agentengine hermes ...`, `agentengine openclaw ...` |

## Examples

The public samples repository is organized by scenario, not only by framework:

- [KSADK Samples](https://github.com/kingsoftcloud/ksadk-samples)
- Knowledge Assistant: RAG and knowledge-base QA.
- Workflow Agent: LangGraph plus AgentEngine toolsets.
- Tool-Using Agent: custom business tools.
- Memory-aware Agent: short-term and long-term memory patterns.

## Deployment

KsADK is local-first, with reviewed deployment entrypoints when you are ready:

```bash
agentengine build .
agentengine launch . --target serverless
agentengine dashboard open
```

When updating existing Hermes or OpenClaw instances, KsADK preserves server-side env, storage, network, and memory configuration by default. Those groups are overwritten only when matching CLI options are provided explicitly.

## Observability

KsADK is OpenTelemetry-native.

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.example.com
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20token
```

Compatible with:

- Langfuse
- Arize
- Datadog
- Grafana
- Phoenix

Export once. Observe anywhere.

## Documentation

- [Getting Started](getting-started/quickstart.en.md)
- [Build](tutorials/langgraph-agent.en.md)
- [Run](guides/local-web-ui.en.md)
- [Deploy](guides/build-and-package.en.md)
- [Observe](guides/observability-tracing.en.md)
- [Extend](guides/tools-and-skill-runtime.en.md)
- [Reference](reference/cli.en.md)

## Community

- Repository: <https://github.com/kingsoftcloud/ksadk-python>
- Wiki: <https://zread.ai/kingsoftcloud/ksadk-python>
- Samples repository: <https://github.com/kingsoftcloud/ksadk-samples>
- Web UI repository: <https://github.com/kingsoftcloud/ksadk-web>
- PyPI: <https://pypi.org/project/ksadk/>
- License: Apache-2.0
