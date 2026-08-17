# Agent-Eval Endpoint Resolution Design

**Goal:** Keep the agent-eval service location out of normal KsADK evaluation commands while preserving explicit overrides for development and private deployments.

## Scope

This change covers endpoint selection for `agentengine evalset push`, `agentengine evalset pull`, and `agentengine eval --dataset-id`. It does not change the Dataset API contract, cloud binding behavior, account selection, or agent-eval deployment topology.

The phase-one delivery remains an internal-network workflow. Public gateway routing and signed external access are a later platform integration, not a blocker for the internal evaluation closed loop.

## CLI Contract

- Users do not need to pass `--agent-eval-url` in normal commands.
- The option is hidden and optional for backward compatibility.
- `AGENT_EVAL_BASE_URL` remains an advanced override for local development, tests, and private deployments.
- All three evaluation entry points use the same resolver and therefore cannot select different cloud environments accidentally.

Example user flow:

```powershell
agentengine evalset push --file support.yaml --dataset-id ds_xxx
agentengine evalset push --file support.yaml
agentengine evalset pull --dataset-id ds_xxx --dataset-version 4 --output-file support-v4.yaml
```

## Endpoint Resolution

Resolution uses the following precedence:

1. A non-empty explicit CLI value or `AGENT_EVAL_BASE_URL` override.
2. The pre-release agent-eval endpoint when `KSYUN_REGION=pre-online`.
3. The online internal agent-eval endpoint for other regions or when the region is absent.

The resolver owns URL trimming and validation. An invalid override fails before any network request. Endpoint constants remain in internal implementation code and must not be copied into public README, changelog, package metadata, or release notes.

## Authentication Boundary

Phase one keeps the current client behavior:

- account identity comes from `AGENT_EVAL_ACCOUNT_ID`, then falls back to `KSYUN_ACCOUNT_ID`;
- the client sends the resolved account as `X-Ksc-Account-Id`;
- an optional bearer token may still be supplied through `AGENT_EVAL_API_TOKEN`;
- no new credential option is exposed by evaluation commands.

This is acceptable only while access is limited to the trusted internal network. The current agent-eval application consumes the account header as request context; it does not establish that the caller owns that account. A public or customer-facing path therefore must go through a trusted gateway that authenticates the caller, rejects replayed or invalid requests, derives authoritative identity, strips spoofable identity headers, and supplies the verified account and region context to agent-eval.

KsADK already has AK/SK configuration and AICP signing behavior for platform APIs. A later gateway integration should reuse those credentials rather than introduce an agent-eval-specific API key. Until that route exists, the internal direct endpoint remains the supported phase-one topology.

## Failure Behavior

- An invalid endpoint override produces a usage error before Dataset conversion or execution.
- A valid endpoint that is unreachable retains the existing agent-eval client error behavior.
- Missing account configuration is not silently replaced with a fabricated identity; agent-eval remains responsible for rejecting operations that require account context.
- Local EvalSet evaluation does not resolve or contact agent-eval.

## Verification

1. CLI help does not expose `--agent-eval-url` for push, pull, or cloud Dataset evaluation.
2. Commands without an endpoint override select pre-release for `KSYUN_REGION=pre-online`.
3. Commands without an endpoint override select online for the normal production region and for an absent region.
4. `AGENT_EVAL_BASE_URL` overrides automatic selection.
5. Push, pull, and fixed-version evaluation construct the client with the same resolved endpoint.
6. Existing account fallback, immutable-version pull, cloud binding, and local-only evaluation tests remain green.

## Delivery Boundary

This change is sufficient for the phase-one internal delivery because the deployed internal Ingress and Dataset APIs already complete the publish, fixed-version read, and local evaluation loop. It is not a claim that agent-eval is ready for direct public Internet access. Public delivery requires gateway routing and verified tenant identity as a separate cross-repository platform task.
