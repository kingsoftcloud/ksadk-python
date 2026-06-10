# Configuration

KsADK reads configuration from command-line options, project YAML, project
`.env`, and optional global configuration. Values closer to the command usually
take precedence for that run.

## Precedence

For a single local run, think about configuration in this order:

1. command-line flags such as `--model` and `--port`.
2. environment variables exported in the shell.
3. values loaded from the project `.env`.
4. project YAML such as `agentengine.yaml`.
5. global developer defaults.

Use command-line flags for temporary experiments and project files for settings
that should travel with the sample application.

## Configuration Sources

| Source | Typical file or command | Purpose |
| --- | --- | --- |
| CLI option | `agentengine run --model glm-5.1` | one-off override |
| Project YAML | `agentengine.yaml` | framework, entry point, region, packaging hints |
| Project env | `.env` | local model credentials and provider URLs |
| Global config | managed by `agentengine config --global` style flows where supported | developer defaults across projects |

## Model Settings

The local runtime uses OpenAI-compatible settings for many examples:

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

Use placeholder values in docs and tests. Use real values only in local `.env`
files or CI secrets.

Common model-related variables:

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | API key for an OpenAI-compatible provider |
| `OPENAI_BASE_URL` | provider base URL, usually ending in `/v1` |
| `OPENAI_MODEL_NAME` | default model for local runs |
| `MODEL_NAME` | compatibility alias used by some projects |

When a request explicitly passes `model`, the local runtime may use it as a
per-request override for supported runners.

## Project Configuration

Project-level settings are stored in `agentengine.yaml`. `ksadk.yaml` and
`ksadk.yml` are also detected for compatibility.

```yaml
name: my-agent
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

Common fields:

| Field | Meaning | Example |
| --- | --- | --- |
| `name` | display name and default runtime name | `my-agent` |
| `framework` | framework adapter | `adk`, `langchain`, `langgraph`, `deepagents` |
| `entry_point` | Python file loaded by the local runtime | `agent.py` |
| `agent_variable` | exported object name | `root_agent` |
| `region` | optional cloud region for deployment-shaped commands | `cn-beijing-6` |

Prefer explicit project YAML in public samples. It is easier for contributors to
review than relying on framework auto-detection.

## Framework-Specific Notes

| Framework | Recommended public config |
| --- | --- |
| ADK | set `framework: adk`, point `entry_point` to the module exporting the ADK agent |
| LangGraph | set `framework: langgraph`, export a compiled graph or provide the configured variable |
| LangChain | set `framework: langchain`, export the chain/runnable object |
| DeepAgents | set `framework: deepagents`, keep service-only startup code out of import side effects |

If the project uses custom state or input preparation, keep those hooks in the
configured entry module so the runner can import them consistently.

## Config Commands

Interactive wizard:

```bash
agentengine config
```

Show effective settings:

```bash
agentengine config show
```

Set values non-interactively:

```bash
agentengine config set region=cn-beijing-6 OPENAI_MODEL_NAME=my-model
```

Switch the default model:

```bash
agentengine config model
```

## Precedence And Local Overrides

Use CLI options for temporary overrides:

```bash
agentengine run . --model another-model
agentengine web . --model another-model
```

This is useful when comparing models without editing `.env`.

## Runtime Feature Flags

Some capabilities are optional. Public examples should make these variables
clearly optional:

| Variable | Purpose |
| --- | --- |
| `KSADK_WORKSPACE_FILES_ENABLED` | enable workspace file routes |
| `KSADK_WORKSPACE_MAX_UPLOAD_BYTES` | set the upload limit for workspace files |
| `KSADK_LTM_BACKEND` | enable long-term memory backend integration |
| `KSADK_LTM_INDEX` | isolate long-term memory data |
| `KSADK_KB_DATASET_ID` | enable a knowledge-base integration |
| `KSADK_KB_TOP_K` | set knowledge retrieval count |
| `KSADK_BUILD_ENABLE_ATTACHMENT_OCR` | include OCR-related dependencies in build flows when intentionally needed |

Leave these unset in the first quickstart unless the page is specifically about
that capability.

## Secrets And Files

Recommended local layout:

```text
my-agent/
  agentengine.yaml
  agent.py
  requirements.txt
  .env              # local only
  .gitignore
```

Recommended `.gitignore` entries:

```gitignore
.env
.agentengine/
dist/
build/
*.egg-info/
```

Never commit `.pypirc`, API keys, cookies, kubeconfig files, private registry
credentials, customer data, local session databases, or uploaded files.

## Public Documentation Rule

Do not publish real internal endpoints, access keys, cookies, kubeconfig paths,
registry names, customer data, or private support URLs in examples.

For public docs:

- use `https://api.example.com/v1` for placeholder provider URLs.
- use `sk-test` or `<YOUR_API_KEY>` for placeholder tokens.
- mark cloud settings as optional.
- prefer local runtime examples over hosted infrastructure examples.
