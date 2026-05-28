# ksadk

[English](README.md) | [简体中文](README.zh-CN.md)

Kingsoft Cloud Agent Development Kit. `ksadk` provides the Python SDK and CLI for building, running, packaging, and deploying AgentEngine agents across local development, serverless runtime, ADK, LangChain/LangGraph, DeepAgents, Hermes, OpenClaw, MCP, and Skill Runtime scenarios.

Current version: `0.6.1`.

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
- Skill Runtime preview: Skill Center discovery, zip download, `sha256` verification, safe extraction, local execution, and sandbox execution through the `ksadk[skills]` extra
- Sandbox Runtime preview: common sandbox abstraction with an E2B-compatible backend

## 0.6.1 Highlights

- OpenAI-compatible `/v1/responses` and `/v1/chat/completions` stay separate externally, while runners receive unified Responses-style canonical input.
- Hosted UI and local `agentengine web` send image/file uploads as Responses `input_image` / `input_file`, with legacy `inlineData` / `fileData` still supported.
- Streaming runs continue in the background after browser refresh or SSE disconnect, and the UI can resubscribe to the same invocation.
- Local web sessions default to project sqlite storage for LangGraph, LangChain, DeepAgents, and ADK when no STM config is set.
- Workspace preview auto-refresh preserves the current preview/edit mode instead of stealing focus.
- Default runtimes use Hermes `2026.5.16-ksadk-v1` and OpenClaw `2026.5.22`.

## Documentation

The public documentation site is hosted on GitHub Pages:

- [Documentation](https://kingsoftcloud.github.io/ksadk-python/)
- [中文文档](https://kingsoftcloud.github.io/ksadk-python/zh/)
- [Quickstart](https://kingsoftcloud.github.io/ksadk-python/getting-started/quickstart/)
- [Configuration](https://kingsoftcloud.github.io/ksadk-python/getting-started/configuration/)
- [CLI Reference](https://kingsoftcloud.github.io/ksadk-python/reference/cli/)
- [OpenAI-compatible API](https://kingsoftcloud.github.io/ksadk-python/reference/openai-compatible-api/)
- [Contributing](https://github.com/kingsoftcloud/ksadk-python/blob/main/CONTRIBUTING.md)
- [Security Policy](https://github.com/kingsoftcloud/ksadk-python/blob/main/SECURITY.md)

The site is built with MkDocs Material, matching the documentation stack used
by Google ADK and Volcengine VEADK.

## Project Links

- Documentation: <https://kingsoftcloud.github.io/ksadk-python/>
- Repository: <https://github.com/kingsoftcloud/ksadk-python>
- Web UI repository: <https://github.com/kingsoftcloud/ksadk-web>

## Notes

- Skill registration, CRUD, and version governance belong to Skill Service. `ksadk` consumes Skill Center at runtime.
- Sandbox template and instance lifecycle belong to Sandbox Service. `ksadk` uses the configured sandbox backend to execute runtime workflows.
- E2B-compatible sandbox backend uses the native `E2B_API_URL` and `E2B_API_KEY` environment variables.
