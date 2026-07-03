# Hermes Troubleshooting

Use these concrete fixes for common Hermes failures.

## `Framework=hermes` returns 422

Meaning: the control-plane schema does not yet accept `hermes`.

Action:

- explain that this is a service-side schema mismatch, not a local CLI problem
- stop retrying the same deploy command
- provide the exact server error back to the user

## `InnerAccountCanOnlyAccessThroughIntranet`

Meaning: the current account must use the intranet control plane.

Action:

- set `AGENTENGINE_SERVER_URL=http://aicp.inner.api.ksyun.com`
- retry against the inner endpoint

## `python-socks is required`

Action:

```bash
pip install python-socks
```

Then retry `agentengine invoke`, `agentengine hermes connect`, or `agentengine hermes pairing`.

## `invoke` returns 401

Meaning: the instance is deployed, but native TUI auth is not ready or no API key was returned for that path.

Action:

- report the instance as deployed
- suggest using the browser chat page as the immediate fallback
- keep the issue separate from deploy success

## `GetAgent` returns 404 right after deploy

Action:

- confirm `.agentengine.state` exists and contains the saved `agent_id`
- retry with explicit `--region pre-online`
- if the instance still cannot be resolved, report a control-plane inconsistency

## Missing `KSYUN_ACCOUNT_ID`

Current behavior:

- permission precheck may be skipped
- deploy and status may still succeed

Do not block the workflow solely because `KSYUN_ACCOUNT_ID` is absent unless the service explicitly rejects the request.
