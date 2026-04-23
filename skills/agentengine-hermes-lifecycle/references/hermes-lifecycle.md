# Hermes Lifecycle

Use this file for the concrete Hermes command sequence.

## Single Instance

1. Verify CLI:

```bash
agentengine --version
```

2. Create project:

```bash
cd /tmp
agentengine init -f hermes <project-name>
cd <project-name>
```

3. Ensure `.env` contains:

```env
OPENAI_API_KEY=...
OPENAI_BASE_URL=http://kspmas.ksyun.com/v1
OPENAI_MODEL_NAME=glm-5.1
AGENTENGINE_SERVER_URL=http://aicp.inner.api.ksyun.com
```

4. Deploy:

```bash
agentengine hermes deploy --name <agent-name> --region pre-online
```

5. Poll status until `RUNNING`:

```bash
agentengine hermes status --region pre-online
```

6. Optional runtime actions:

```bash
agentengine invoke
agentengine hermes connect --region pre-online
agentengine hermes pairing --region pre-online -- list
agentengine hermes open --chat --region pre-online
agentengine hermes open --manage --region pre-online
```

## Batch Creation

Use one temp directory per instance:

- `/tmp/<prefix>-01`
- `/tmp/<prefix>-02`
- `/tmp/<prefix>-03`

For each instance:

1. `init -f hermes`
2. patch `.env`
3. `hermes deploy --name <name> --region pre-online`
4. `hermes status --region pre-online` until `RUNNING`
5. create permanent links

Keep the final result grouped by instance name.

## Notes

- `agentengine invoke <hermes-agent>` defaults to Hermes native remote TUI.
- `agentengine invoke <hermes-agent> -m "hello"` is a different path and should not replace native TUI validation.
- `connect` enters the hosted gateway setup flow and is expected to be interactive.
