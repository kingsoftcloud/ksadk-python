# ksadk

[简体中文](README.md) | [English](README.en.md)

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/kingsoftcloud/ksadk-python)

Kingsoft Cloud Agent Development Kit. `ksadk` provides the Python SDK and command line tools for building, running, packaging, and deploying AgentEngine agents. After installation, both `agentengine` and the equivalent `ksadk` command are available. KsADK covers local development, serverless runtime, ADK, LangChain/LangGraph, DeepAgents, Hermes, OpenClaw, MCP, and Skill Runtime scenarios.

Current version: `0.6.2`.

## Install

```bash
pip install -U ksadk
```

Install optional runtime extras when needed:

```bash
pip install -U "ksadk[adk]"
pip install -U "ksadk[langgraph]"
pip install -U "ksadk[deepagents]"
pip install -U "ksadk[skills]"
pip install -U "ksadk[all]"
```

## Quick Start

The examples below use `agentengine`; you can replace it with `ksadk` for the same CLI.

Create and run a local agent:

```bash
agentengine init my-agent -f langgraph
cd my-agent
agentengine config
agentengine run -i
```

Deploy to AgentEngine serverless runtime:

```bash
agentengine launch . --target serverless
```

Open the hosted dashboard:

```bash
agentengine dashboard open
```

## What Is Included

- Local development commands: `init`, `config`, `run`, `web`
- Build and deploy commands: `build`, `deploy`, `launch`
- Remote operations: `agent invoke`, `files`, `dashboard`
- Runtime integrations: ADK, LangChain, LangGraph, DeepAgents, MCP
- Hosted runtime assets: Hermes and OpenClaw
- Skill Runtime: Skill Space discovery, zip download, `sha256` verification, safe extraction, instruction loading, and workflow execution through `local_process` or E2B sandbox backends
- Built-in AgentEngine tools: skill discovery/loading, workspace file operations, component status, sandbox status, and sandbox direct code/command execution
- Sandbox Runtime: common sandbox abstraction with an E2B-compatible backend

## 0.6.2 Highlights

- Skill Runtime can discover Skill Space entries, download and verify skill packages, load `SKILL.md`, and execute workflow-style skills through `local_process` or E2B sandbox backends.
- `ksadk.toolsets` provides Skill, Workspace, Platform, and Sandbox built-in tools; the recommended binding pattern is `get_agentengine_tools(include=["focused", "agentengine_tool_dispatcher"])`, with lower-frequency or higher-risk tools called through dispatcher `list` / `describe` / `call`.
- Tool Gateway provides a shared `approval_required` envelope for medium/high-risk operations such as workspace writes/deletes, Skill Runtime execution, and sandbox command/code execution.
- Workspace tools now include exact snippet editing and lightweight lint checks; Sandbox tools add direct `run_command` / `run_code` and only execute through the configured isolated sandbox backend.
- `setup_tracing()` now prefers standard `OTEL_EXPORTER_OTLP_*` HTTP traces configuration while existing Langfuse environment variables remain compatible.
- The environment registry and public docs cover OTLP traces, AICP endpoint mode, Skill Service endpoint/scheme overrides, Sandbox Runtime, Skill Runtime, and Tool Gateway settings.

## Documentation

The public documentation site is hosted on GitHub Pages:

- [Documentation](https://kingsoftcloud.github.io/ksadk-python/en/)
- [中文文档](https://kingsoftcloud.github.io/ksadk-python/zh/)
- [Quickstart](https://kingsoftcloud.github.io/ksadk-python/en/getting-started/quickstart/)
- [Configuration](https://kingsoftcloud.github.io/ksadk-python/en/getting-started/configuration/)
- [CLI Reference](https://kingsoftcloud.github.io/ksadk-python/en/reference/cli/)
- [OpenAI-compatible API](https://kingsoftcloud.github.io/ksadk-python/en/reference/openai-compatible-api/)
- [Contributing](https://github.com/kingsoftcloud/ksadk-python/blob/main/CONTRIBUTING.md)
- [Security Policy](https://github.com/kingsoftcloud/ksadk-python/blob/main/SECURITY.md)

The site is built with MkDocs Material, matching the documentation stack used
by Google ADK and Volcengine VEADK.

## Project Links

- Documentation: <https://kingsoftcloud.github.io/ksadk-python/en/>
- Repository: <https://github.com/kingsoftcloud/ksadk-python>
- Samples repository: <https://github.com/kingsoftcloud/ksadk-samples>
- Web UI repository: <https://github.com/kingsoftcloud/ksadk-web>
- PyPI: <https://pypi.org/project/ksadk/>

## Notes

- Skill registration, CRUD, and version governance belong to Skill Service. `ksadk` consumes Skill Center at runtime.
- Sandbox template and instance lifecycle belong to Sandbox Service. `ksadk` uses the configured sandbox backend to execute runtime workflows.
- E2B-compatible sandbox backend uses the native `E2B_API_URL` and `E2B_API_KEY` environment variables.
