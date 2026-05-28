# CLI Reference

`agentengine` is the public command-line entry point for KsADK.

```bash
agentengine [OPTIONS] COMMAND [ARGS]...
```

Global options:

| Option | Meaning |
| --- | --- |
| `--output pretty/json` | choose human-readable or JSON output where supported. |
| `--no-color` | disable colored terminal output. |
| `--dry-run` | print planned requests without executing supported operations. |
| `--version` | show the package version. |
| `-h, --help` | show help. |

## Core Commands

```bash
agentengine --help
agentengine init --help
agentengine run --help
agentengine web --help
```

## Command Groups

| Group | Public role | Local-first? |
| --- | --- | --- |
| `init` | create or wrap a project | yes |
| `config` | manage model and project settings | yes |
| `run` | run terminal loop or local API server | yes |
| `web` | start local browser UI | yes |
| `a2a` | expose A2A surfaces where configured | yes |
| `mcp` | build or manage MCP resources | depends on target |
| `build` | prepare deployment artifacts | depends on target |
| `deploy` | deploy to configured cloud runtime | no |
| `launch` | build and deploy in one command | no |
| `agent` | manage hosted Agent resources | no |
| `dashboard` | open hosted dashboard links | no |
| `files` | manage hosted workspace files | no |
| `version` | manage hosted Agent versions | no |
| `hermes` | manage Hermes resources | no |
| `openclaw` | manage OpenClaw resources | no |

Public quickstarts should focus on local-first commands. Hosted command docs
must state their credential and infrastructure prerequisites.

## Local Development

### `agentengine init`

Create a new project.

```bash
agentengine init my-agent -f langgraph
agentengine init my-agent -f adk
agentengine init my-agent --from-agent ./existing_agent.py
```

Supported framework flags include `adk`, `langchain`, `langgraph`,
`deepagents`, `openclaw`, and `hermes`. Public examples should prefer the
frameworks that can run locally without internal infrastructure.

After importing existing code, inspect `agentengine.yaml` before running.

### `agentengine config`

Manage project and model settings.

```bash
agentengine config
agentengine config show
agentengine config set OPENAI_MODEL_NAME=my-model
agentengine config model
```

### `agentengine run`

Run an agent project.

```bash
agentengine run .
agentengine run . -i
agentengine run . --port 8080
agentengine run . --model my-model
```

Common options:

| Option | Meaning |
| --- | --- |
| `--port` | local server port, default `8080`. |
| `--interactive` / `-i` | terminal interactive mode. |
| `--model` | one-run model override. |
| `--show-thinking` | display provider reasoning output when available. |
| `--no-stream` | disable streaming rendering. |
| `--no-trace` | disable tracing. |

Use interactive mode for manual testing and server mode for API clients:

```bash
agentengine run . -i
agentengine run . --port 8080
```

### `agentengine web`

Start the local invoke/debug UI.

```bash
agentengine web .
agentengine web . --port 7860
agentengine web . --model my-model
agentengine web . --no-open
```

## Protocol And Integration

### `agentengine a2a`

Expose A2A protocol surfaces and Agent Card metadata.

```bash
agentengine a2a card --help
agentengine a2a serve --help
```

### `agentengine mcp`

Manage MCP-related resources and runtime flows where configured.

```bash
agentengine mcp --help
```

## Build And Hosted Operations

These commands are part of the SDK surface, but may require Kingsoft Cloud
credentials or approved hosted infrastructure.

```bash
agentengine build --help
agentengine deploy --help
agentengine launch --help
agentengine agent --help
agentengine dashboard --help
agentengine files --help
agentengine hermes --help
agentengine openclaw --help
```

Use `--dry-run` where supported when documenting or reviewing deployment-shaped
commands.

## JSON Output

Some commands support structured output:

```bash
agentengine --output json agent status
agentengine --output json build --help
```

Interactive commands and browser UI commands may reject JSON output when a
structured response would be misleading.

## Public Release Gate

Before publishing CLI docs, regenerate help output from the release candidate and
check it for internal URLs, credentials, private registry names, kubeconfig
paths, and deployment assumptions.

Recommended checks:

```bash
agentengine --help
agentengine init --help
agentengine run --help
agentengine web --help
agentengine config --help
```
