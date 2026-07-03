# OpenClaw Troubleshooting

## `InnerAccountCanOnlyAccessThroughIntranet`

Use the intranet control plane for intranet-only accounts and avoid retrying the same public endpoint.

## Missing Python or stale `ksadk`

Prefer upgrading `ksadk` before debugging OpenClaw behavior:

```bash
pip install -U ksadk
```

## Dashboard link created but expiry unclear

The `dashboard open --output json` response may not directly show `永久`. Always confirm with:

```bash
agentengine dashboard share list --agent <agent-id-or-name> --output json
```

## Batch mode and interactive connect

If the user asks for batch deployment plus channel onboarding:

- finish the batch deploy first
- summarize all instance IDs and dashboard URLs
- switch to a single-instance `channel connect` workflow afterward
