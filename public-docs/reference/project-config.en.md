# Project Configuration

KsADK project configuration is normally stored in `agentengine.yaml`.
`ksadk.yaml` and `ksadk.yml` are accepted for compatibility, but new public
examples should use `agentengine.yaml`.

## Minimal Config

```yaml
name: my-agent
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | recommended | display name and default agent name |
| `framework` | recommended | adapter family: `adk`, `langgraph`, `langchain`, `deepagents` |
| `entry_point` | recommended | Python file loaded by the local runtime |
| `agent_variable` | optional | exported object name, default `root_agent` |
| `package` | optional | package directory when different from project name |
| `region` | optional | cloud region for deployment-shaped commands |
| `artifact_type` | optional | packaging mode for specialized runtimes |

## Framework Values

| Value | Typical exported object |
| --- | --- |
| `adk` | `root_agent = Agent(...)` |
| `langgraph` | `root_agent = graph.compile()` |
| `langchain` | runnable chain or agent |
| `deepagents` | `root_agent = create_deep_agent(...)` |

Public examples should avoid specialized hosted runtime values unless the guide
also explains public credentials, public runtime images, and local fallback
behavior.

## Environment Variables

Model settings usually live in `.env`:

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

Local UI session storage can be controlled with:

```bash
KSADK_STM_BACKEND=sqlite
KSADK_STM_PATH=.agentengine/ui/sessions.sqlite
```

Only commit `.env.example` files with placeholders. Do not commit `.env`.

## Detection Fallback

If no config file exists, KsADK tries to infer project shape from:

- `langgraph.json`
- a package matching the project name.
- a package containing `__init__.py`.
- root-level `agent.py`, `main.py`, or `app.py`.
- common agent variables in source.

Inference is convenient for local experiments, but explicit config is better for
docs, samples, tests, and release candidates.

## Validation Checklist

Before publishing a project:

- `entry_point` exists.
- `agent_variable` is exported by the entry point.
- `.env` is ignored by Git.
- placeholder provider values are used in docs.
- cloud fields are optional or documented with public prerequisites.
- `agentengine run . -i` works from a clean checkout.
