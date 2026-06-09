# ksadk

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

- Skill Runtime can discover Skill Space entries, download and verify skill packages, load `SKILL.md`, and execute workflow-style skills through `local_process` or E2B sandbox backends.
- Built-in AgentEngine tools are available through `ksadk.toolsets`, including skill discovery/loading, workspace file tools, component status, sandbox status, and sandbox direct `run_command` / `run_code`.
- `get_agentengine_tools(include=["focused", "agentengine_tool_dispatcher"])` is the recommended binding pattern for LangGraph/LangChain-style agents that need progressive disclosure instead of binding every built-in tool into the model context.
- Tool Gateway provides a shared approval envelope for medium/high-risk operations such as workspace writes/deletes, Skill Runtime execution, and sandbox command/code execution.
- Workspace tools now include exact snippet editing and lightweight lint checks in addition to list/read/write/search/delete operations.
- `setup_tracing()` now recognizes standard `OTEL_EXPORTER_OTLP_*` HTTP traces settings, while existing Langfuse environment variables remain compatible.
- Environment variable registry and docs now cover OTLP traces settings, AICP endpoint mode, Skill Service endpoint/scheme overrides, Sandbox Runtime, and Skill Runtime settings.

## 0.6.0 Highlights

- OpenAI-compatible `/v1/responses` and `/v1/chat/completions` stay separate externally, while runners receive unified Responses-style canonical input.
- Hosted UI and local `agentengine web` send image/file uploads as Responses `input_image` / `input_file`, with legacy `inlineData` / `fileData` still supported.
- Streaming runs continue in the background after browser refresh or SSE disconnect, and the UI can resubscribe to the same invocation.
- Local web sessions default to project sqlite storage for LangGraph, LangChain, DeepAgents, and ADK when no STM config is set.
- Workspace preview auto-refresh preserves the current preview/edit mode instead of stealing focus.
- Default runtimes use Hermes `2026.5.16-ksadk-v1` and OpenClaw `2026.5.22`.

## Documentation

PyPI does not serve repository-relative files such as `./docs/*.md`. The links below use absolute URLs so they render correctly on both PyPI and GitHub.

- [Usage Guide](https://github.com/kingsoftcloud/ksadk-python/blob/master/docs/ksadk%E4%BD%BF%E7%94%A8%E6%96%87%E6%A1%A3.md)
- [Technical Design](https://github.com/kingsoftcloud/ksadk-python/blob/master/docs/ksadk%E6%8A%80%E6%9C%AF%E8%AE%BE%E8%AE%A1.md)
- [Workspace Files Design](https://github.com/kingsoftcloud/ksadk-python/blob/master/docs/%E5%B7%A5%E4%BD%9C%E5%8C%BA%E6%96%87%E4%BB%B6%E6%8A%80%E6%9C%AF%E8%AE%BE%E8%AE%A1.md)
- [Memory Guide](https://github.com/kingsoftcloud/ksadk-python/blob/master/docs/%E8%AE%B0%E5%BF%86%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97.md)
- [Knowledge Base and Memory Examples](https://github.com/kingsoftcloud/ksadk-python/blob/master/docs/%E7%9F%A5%E8%AF%86%E5%BA%93%E4%B8%8E%E8%AE%B0%E5%BF%86%E7%A4%BA%E4%BE%8B.md)
- [DeepAgents Guide](https://github.com/kingsoftcloud/ksadk-python/blob/master/docs/DeepAgents%E8%AF%B4%E6%98%8E.md)
- [Hermes Runtime Guide](https://github.com/kingsoftcloud/ksadk-python/blob/master/deploy/hermes/README.md)
- [OpenClaw Deployment Guide](https://github.com/kingsoftcloud/ksadk-python/blob/master/docs/openclaw%E4%B8%80%E9%94%AE%E9%83%A8%E7%BD%B2%E6%8C%87%E5%8D%97.md)
- [OpenClaw User Image Template](https://github.com/kingsoftcloud/ksadk-python/blob/master/deploy/openclaw-user-template/README.md)
- [Skill Runtime Image Contract](https://github.com/kingsoftcloud/ksadk-python/blob/master/deploy/skill-runtime/README.md)

## Project Links

- Documentation: <https://ksadk.kingsoft.com/docs>
- Repository: <https://github.com/kingsoftcloud/ksadk-python>

## Notes

- Skill registration, CRUD, and version governance belong to Skill Service. `ksadk` consumes Skill Center at runtime.
- Sandbox template and instance lifecycle belong to Sandbox Service. `ksadk` uses the configured sandbox backend to execute runtime workflows.
- E2B-compatible sandbox backend uses the native `E2B_API_URL` and `E2B_API_KEY` environment variables.
