# KsADK

[简体中文](README.md) | [English](README.en.md)

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/kingsoftcloud/ksadk-python)

Build agents once. Run them anywhere.

KsADK is the Agent Runtime Platform for AI agents. Build with Google ADK,
LangGraph, LangChain, or DeepAgents, then run, debug, expose, observe, and
deploy those agents through one runtime experience.

The default README is Chinese-first. This English README is maintained as a
companion entrypoint.

![Real KsADK CLI screenshot: agentengine -h](public-docs/assets/ksadk-runtime-platform-hero.png)

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

Start the real local Web UI:

```bash
agentengine web . --no-open
```

![Real KsADK Web UI debugging screenshot](public-docs/assets/ksadk-web-ui-screenshot.png)

![Real local Web UI demo GIF](public-docs/assets/ksadk-local-debugging-demo.gif)

Common configuration:

```bash
# Only set this for non-default OpenAI-compatible endpoints.
agentengine config set OPENAI_BASE_URL=https://api.example.com/v1

# Set this when using Kingsoft Cloud AgentEngine, Skill Service, knowledge base, or long-term memory.
agentengine config set KSYUN_REGION=cn-beijing-6
```

## Why KsADK

Most agent frameworks solve how to build agents. KsADK solves how to run, debug,
deploy, and observe them.

- Unified CLI: `agentengine init`, `agentengine run`, `agentengine web`.
- Unified debugging: browser Web UI, streaming, attachments, workspace files,
  tool calls, and sessions.
- Unified protocol: local `/v1/responses` and `/v1/chat/completions`.
- Unified tool boundary: Skill Runtime, Workspace, Sandbox, Memory, Knowledge.
- Unified engineering path: packaging, deployment, OpenTelemetry observability.

## Architecture

![KsADK Agent Runtime Platform architecture](public-docs/assets/ksadk-runtime-architecture.png)

For details, read [Why KsADK](https://kingsoftcloud.github.io/ksadk-python/en/getting-started/why-ksadk/),
[Architecture](https://kingsoftcloud.github.io/ksadk-python/en/getting-started/architecture/), and
[Ecosystem Positioning](https://kingsoftcloud.github.io/ksadk-python/en/getting-started/comparison/).

The positioning page compares KsADK with Google ADK, LangGraph, OpenAI Agents
SDK, VEADK, and AgentRun factually, without misleading feature scorecards.

## Core Capabilities

| Capability | Entry points |
| --- | --- |
| Local Development | `agentengine init`, `agentengine config`, `agentengine run` |
| Browser Debugging UI | `agentengine web` |
| OpenAI-Compatible API | `/v1/responses`, `/v1/chat/completions` |
| Multi-framework runtime | ADK / LangGraph / LangChain / DeepAgents runners |
| Tools and isolated execution | `ksadk.toolsets`, Skill Runtime, Workspace, Sandbox |
| Optional Markdown repair | `ksadk.markdown.repair_markdown` optional app-side repair; runtime does not rewrite raw model output by default |
| Deployment and Observability | Serverless / Hermes / OpenClaw, OpenTelemetry |

## Examples

- [KSADK Samples](https://github.com/kingsoftcloud/ksadk-samples)
- Knowledge Assistant: RAG and knowledge-base QA.
- Workflow Agent: LangGraph plus AgentEngine toolsets.
- Tool-Using Agent: custom tool calls.
- Memory-aware Agent: short-term and long-term memory patterns.

## Documentation And Community

- Documentation: <https://kingsoftcloud.github.io/ksadk-python/>
- CLI reference: <https://kingsoftcloud.github.io/ksadk-python/en/reference/cli/>
- Environment variables: <https://kingsoftcloud.github.io/ksadk-python/en/reference/environment-variables/>
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- GitHub Releases: <https://github.com/kingsoftcloud/ksadk-python/releases>
- Wiki: <https://zread.ai/kingsoftcloud/ksadk-python>
- Web UI repository: <https://github.com/kingsoftcloud/ksadk-web>
- PyPI: <https://pypi.org/project/ksadk/>

## Contributing

Issues, pull requests, samples, and documentation improvements are welcome. Run
the public gate before submitting:

```bash
make public-preflight
```

License: Apache-2.0.
