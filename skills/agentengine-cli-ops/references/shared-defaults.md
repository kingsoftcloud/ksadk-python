# Shared Defaults

Apply these defaults unless the user explicitly overrides them.

## Pre-flight Check

Before any deploy, verify credentials are configured:

```bash
agentengine config show
```

If missing required credentials, prompt user to run:

```bash
agentengine config wizard
```

See [prerequisites.md](./prerequisites.md) for full credential requirements.

## Install and CLI Checks

- Preferred install command: `pip install -U ksadk`
- Require `ksadk >= 0.5.0` for Hermes support
- Verify CLI first with `agentengine --version`

## Regions

- Hermes default region: `pre-online`
- OpenClaw default region: `cn-beijing-6`

## Model Defaults

When the user did not provide model settings and the workflow needs hosted runtime defaults:

- `OPENAI_BASE_URL=http://kspmas.ksyun.com/v1`
- `OPENAI_MODEL_NAME=glm-5.1`

## Control Plane Defaults

- Hermes default control-plane URL: `AGENTENGINE_SERVER_URL=http://aicp.inner.api.ksyun.com`
- If the account is intranet-only, prefer the inner control plane over the public endpoint

## Working Directories

- Hermes without a user-specified directory: `/tmp/<agent-name>`
- Hermes batch mode: one directory per instance under `/tmp`
- OpenClaw without project init needs: use a temp working directory and keep the resulting state file with the instance runbook

## Naming

- Hermes default single-instance name: `my-hermes-<date>-01`
- Hermes batch names: `<prefix>-01`, `<prefix>-02`, `<prefix>-03`
- OpenClaw default single-instance name: `openclaw-gateway-<timestamp>`
- OpenClaw batch names: `<prefix>-01`, `<prefix>-02`, `<prefix>-03`

## Batch Output

Hermes batch output must always include:

- `name`
- `agent_id`
- `status`
- `chat_url`
- `manage_url`

OpenClaw batch output must always include:

- `name`
- `agent_id`
- `status`
- `dashboard_url`
