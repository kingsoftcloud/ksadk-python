# OpenClaw Dashboard Links

Create a permanent OpenClaw dashboard link with:

```bash
agentengine dashboard open \
  --share \
  --expires-seconds 0 \
  --no-open \
  --output json
```

If the instance is not implied by the current directory, pass:

```bash
agentengine dashboard open \
  --agent <agent-id-or-name> \
  --share \
  --expires-seconds 0 \
  --no-open \
  --output json
```

## Validation

Always validate permanence with:

```bash
agentengine dashboard share list --agent <agent-id-or-name> --output json
```

Validation passes only if:

- the expected dashboard link is present
- the link status is `active`
- `expires_at=永久`
