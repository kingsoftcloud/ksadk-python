# Quickstart

This quickstart creates a local LangGraph agent, configures an
OpenAI-compatible model provider, runs the agent in the terminal, and opens the
local Web UI.

The flow is local-first. It does not require internal Kingsoft Cloud accounts,
private gateways, internal object storage, or private Kubernetes clusters.

## Requirements

- Python 3.10 or newer.
- A shell with `python` and `pip`.
- An OpenAI-compatible chat model endpoint and API key that you control.

Optional framework extras can be installed as needed. The default quickstart uses
LangGraph.

## Create A Clean Workspace

```bash
mkdir ksadk-quickstart
cd ksadk-quickstart
```

Keep the virtual environment inside the workspace while learning. For production
projects, use your team's normal Python environment manager.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install ksadk
```

Install a framework extra when your project needs it:

```bash
pip install "ksadk[langgraph]"
```

Check that the CLI is installed:

```bash
agentengine --help
agentengine --version
```

## Create A Project

```bash
agentengine init my-agent -f langgraph
cd my-agent
```

The generated project contains an agent entry file and a project configuration
file. See [Project Structure](project-structure.md) for details.

Expected files:

```text
my-agent/
  agent.py
  agentengine.yaml
```

## Configure A Model

Use the non-interactive config command for reproducible setup:

```bash
agentengine config set \
  OPENAI_API_KEY=sk-test \
  OPENAI_BASE_URL=https://api.example.com/v1 \
  OPENAI_MODEL_NAME=my-model
```

Use real provider values only in your local `.env`. Do not commit `.env`.

Check the effective configuration:

```bash
agentengine config show
```

You can also run the interactive wizard:

```bash
agentengine config
```

## Inspect The Agent

Open `agent.py` and confirm that the configured agent variable exists. The
default generated LangGraph project should expose:

```python
root_agent = graph.compile()
```

Open `agentengine.yaml` and confirm:

```yaml
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

## Run In The Terminal

```bash
agentengine run . -i
```

Useful flags:

- `--model <name>` overrides the configured model for one run.
- `--show-thinking` displays model reasoning output when the provider returns it.
- `--no-stream` waits for a complete response before rendering.
- `--no-trace` disables tracing.

Send a basic prompt:

```text
What can this agent do?
```

If the model provider is reachable, the CLI should stream or print a response.
If it fails, check [Troubleshooting](../reference/troubleshooting.md#model-calls-fail).

## Start The Local Web UI

```bash
agentengine web . --no-open
```

The command prints a local URL. Open it in a browser and send a test message to
the agent. `agentengine web` uses static assets bundled in the Python package, so
end users do not need Node.js.

The local UI stores browser debugging state under `.agentengine/` by default.
Do not commit that directory.

## Start A Local API Server

```bash
agentengine run . --port 8080
```

Then call the local OpenAI-compatible endpoint:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "my-agent",
    "messages": [
      {"role": "user", "content": "Say hello from KsADK"}
    ],
    "stream": false
}'
```

Call the Responses endpoint:

```bash
curl http://127.0.0.1:8080/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "my-agent",
    "input": "Return a one sentence status.",
    "stream": false
  }'
```

Use `stream: true` only when your client can consume server-sent events.

## Stop Local Processes

Press `Ctrl+C` in each terminal running `agentengine run` or `agentengine web`.

## What You Have Built

You now have:

- a local Python agent project.
- explicit KsADK project configuration.
- local model settings in `.env`.
- a terminal loop for quick tests.
- a browser UI for debugging sessions.
- an OpenAI-compatible local HTTP server for client integration.

## Next Steps

- Build a complete example in [Build A LangGraph Agent](../tutorials/langgraph-agent.md).
- Wrap an existing project in [Bring An Existing Agent](../tutorials/existing-agent.md).
- Configure more settings in [Configuration](configuration.md).
- Learn framework conventions in [Frameworks](../guides/frameworks.md).
- Debug with the [Local Web UI](../guides/local-web-ui.md).
- Check commands in the [CLI Reference](../reference/cli.md).
