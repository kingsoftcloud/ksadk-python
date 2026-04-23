# OpenClaw Lifecycle

Use this file for concrete OpenClaw CLI flows.

## Single Instance

1. Verify CLI:

```bash
agentengine --version
```

2. Confirm current config if needed:

```bash
agentengine config show --output json
```

3. Deploy:

```bash
agentengine openclaw deploy --name <name> --region cn-beijing-6
```

4. Check status:

```bash
agentengine openclaw status <agent-id-or-name>
```

5. Create permanent dashboard link after the instance is healthy.

## Batch Creation

Use batch mode for deploy, status, and dashboard links only.

Suggested names:

- `<prefix>-01`
- `<prefix>-02`
- `<prefix>-03`

For each instance:

1. deploy
2. wait until the status is usable
3. create a permanent dashboard link
4. summarize the result as `name / agent_id / status / dashboard_url`

Do not enter `channel connect` inside the batch loop.
