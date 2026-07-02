# Project Structure

`agentengine init` creates a small project that can be run locally and later
packaged for deployment.

## Typical Layout

```text
my-agent/
  agent.py
  agentengine.yaml
  .env
```

Some projects use a package layout:

```text
my-agent/
  my_agent/
    __init__.py
    agent.py
  agentengine.yaml
  .env
```

KsADK also supports `ksadk.yaml` and `ksadk.yml` for compatibility.

## `agent.py`

The agent entry module should export the object configured as `agent_variable`.
The default variable is `root_agent`.

```python
root_agent = graph.compile()
```

For ADK projects this is usually a `google.adk.agents.Agent`. For LangGraph it
is commonly a compiled graph. For LangChain it may be a runnable chain. For
DeepAgents it is the object returned by `create_deep_agent`.

## `agentengine.yaml`

The project YAML makes framework detection explicit.

```yaml
name: my-agent
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

Supported public framework values include:

- `adk`
- `langchain`
- `langgraph`
- `deepagents`

## `.env`

The `.env` file is for local secrets and provider settings. Keep it out of Git.

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

For public docs and examples, use placeholders only. Do not publish real tokens,
internal endpoints, private registry names, cookies, kubeconfig paths, or
customer data.

## Generated Files

Local runs may create caches, virtual environments, build output, or runtime
state. These are not source files:

- `.venv/`
- `__pycache__/`
- `.pytest_cache/`
- `dist/`
- `build/`
- `site/`
- `.agentengine/`
- `.agentengine.state`

## Importing An Existing Agent

Use `--from-agent` when you already have a Python file or directory:

```bash
agentengine init my-agent --from-agent ./existing_agent.py
agentengine init my-agent --from-agent ./existing_agent_dir
```

After import, inspect `agentengine.yaml` and confirm the detected framework,
entry point, and exported variable before running the project.
