# ksadk

[简体中文](README.md) | [English](README.en.md)

Kingsoft Cloud Agent Development Kit. `ksadk` provides the Python SDK and CLI for building, running, packaging, and deploying AgentEngine agents across local development, serverless runtime, ADK, LangChain/LangGraph, DeepAgents, Hermes, OpenClaw, MCP, and Skill Runtime scenarios.

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

## 0.6.2 Highlights

- `setup_tracing()` now prefers standard `OTEL_EXPORTER_OTLP_*` HTTP traces configuration, so spans can be routed to Langfuse or any OTLP Collector.
- Langfuse environment variables remain compatible; automatic mode avoids enabling the Langfuse direct exporter again when generic OTLP is already configured.
- Tracing docs now explain span event versus child span visibility in backends and recommend `score.*` attributes for evaluation scores.
- Skill Runtime keeps public Skill Space, allowlist, E2B, and Sandbox backend support in the public SDK surface.
- 0.6.1 behavior remains available, including Responses input semantics, streaming session recovery, local sqlite sessions, and workspace preview refinements.

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
- Web UI repository: <https://github.com/kingsoftcloud/ksadk-web>
- PyPI: <https://pypi.org/project/ksadk/>

## Notes

- Skill registration, CRUD, and version governance belong to Skill Service. `ksadk` consumes Skill Center at runtime.
- Sandbox template and instance lifecycle belong to Sandbox Service. `ksadk` uses the configured sandbox backend to execute runtime workflows.
- E2B-compatible sandbox backend uses the native `E2B_API_URL` and `E2B_API_KEY` environment variables.
