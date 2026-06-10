<h1 align="center">KsADK</h1>

<p align="center"><strong>Build agents once. Run them anywhere.</strong></p>

<p align="center">
  KsADK is the Agent Runtime Platform for AI agents.
  Keep building with Google ADK, LangGraph, LangChain, or DeepAgents, then run, debug, expose, sandbox, deploy, and observe them through one runtime experience.
</p>

<p align="center"><a href="README.md">简体中文</a> · <a href="README.en.md">English</a></p>

<p align="center">
  <a href="https://kingsoftcloud.github.io/ksadk-python/"><img alt="Docs" src="https://img.shields.io/badge/Docs-ksadk--python-2f6fdf?style=flat" /></a>
  <a href="https://pypi.org/project/ksadk/"><img alt="PyPI" src="https://img.shields.io/pypi/v/ksadk?style=flat&color=2f6fdf" /></a>
  <a href="https://zread.ai/kingsoftcloud/ksadk-python"><img alt="Ask Zread" src="https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff" /></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-Apache--2.0-blue?style=flat" /></a>
</p>

<p align="center"><a href="public-docs/assets/ksadk-runtime-platform-hero.png"><img alt="Real KsADK CLI screenshot: agentengine -h" src="public-docs/assets/ksadk-runtime-platform-hero-wide.png" width="860" /></a></p>

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

Start the local debugging Web UI:

```bash
agentengine web . --no-open
```

<p align="center"><img alt="Real KsADK Web UI debugging screenshot" src="public-docs/assets/ksadk-web-ui-screenshot.png" width="860" /></p>

<p align="center"><img alt="Real local Web UI demo" src="public-docs/assets/ksadk-local-debugging-demo.gif" width="860" /></p>

## Why KsADK

Most agent frameworks solve how to build agents. KsADK solves how to run, debug, deploy, and observe them.

- Local development: `agentengine init`, `agentengine run`, `agentengine web`.
- Unified debugging: browser Web UI, streaming, attachments, workspace files, tool calls, and sessions.
- Unified protocol: local `/v1/responses` and `/v1/chat/completions`.
- Tool boundaries: Skill Runtime, Workspace, Sandbox, Memory, Knowledge.
- Engineering workflow: packaging, deployment, OpenTelemetry observability.

## Architecture

<p align="center"><img alt="KsADK Agent Runtime Platform architecture" src="public-docs/assets/ksadk-runtime-architecture.png" width="860" /></p>

## Docs And Examples

- Documentation: <https://kingsoftcloud.github.io/ksadk-python/>
- Quick Start: <https://kingsoftcloud.github.io/ksadk-python/en/getting-started/quickstart/>
- Why KsADK: <https://kingsoftcloud.github.io/ksadk-python/en/getting-started/why-ksadk/>
- Architecture: <https://kingsoftcloud.github.io/ksadk-python/en/getting-started/architecture/>
- Ecosystem Positioning: <https://kingsoftcloud.github.io/ksadk-python/en/getting-started/comparison/>
- Observability: <https://kingsoftcloud.github.io/ksadk-python/en/guides/observability-tracing/>
- Samples: <https://github.com/kingsoftcloud/ksadk-samples>

## Related Projects

- KsADK repository: <https://github.com/kingsoftcloud/ksadk-python>
- Web UI repository: <https://github.com/kingsoftcloud/ksadk-web>
- Wiki: <https://zread.ai/kingsoftcloud/ksadk-python>
- PyPI: <https://pypi.org/project/ksadk/>

## Contributing

Issues, pull requests, samples, and documentation improvements are welcome. Before submitting, run:

```bash
make public-preflight
```

License: Apache-2.0.
