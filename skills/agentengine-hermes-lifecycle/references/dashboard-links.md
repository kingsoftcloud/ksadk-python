# Dashboard Links

Create two permanent Hermes links after the instance reaches `RUNNING`.

## Create Chat Link

```bash
agentengine dashboard open \
  --path /chat \
  --share \
  --expires-seconds 0 \
  --no-open \
  --region pre-online \
  --output json
```

## Create Management Link

```bash
agentengine dashboard open \
  --path / \
  --share \
  --expires-seconds 0 \
  --no-open \
  --region pre-online \
  --output json
```

## Validate Permanence

The immediate `dashboard open --output json` response may still show `expires_at=server-default`. Always run:

```bash
agentengine dashboard share list --agent <agent-id> --region pre-online --output json
```

Validation passes only if:

- exactly one link shows `path=/chat`
- exactly one link shows `path=/`
- both links show `expires_at=永久`

## Final Output Fields

Return:

- `chat_url`
- `manage_url`
- optional `chat_link_id`
- optional `manage_link_id`
