# Agent-Eval KOP Default Transport Design

**Goal:** Make signed AICP/KOP the default transport for cloud EvalSet commands while preserving an explicit direct-HTTP override for development and private deployments.

## Scope

This change covers cloud EvalSet publish, pull, catalog, `agentengine eval --dataset-id`, and Studio cloud Dataset reads. It does not change the Dataset contract, CloudBinding format, immutable-version semantics, or agent-eval deployment topology.

The phase-one delivery uses the existing AICP/KOP gateway and KsADK's global AK/SK configuration. `PublishEvaluationSetSnapshot` must be registered in KOP before this client path can succeed in pre-release or online environments.

## User Contract

- Normal users do not pass an agent-eval endpoint.
- The CLI does not expose `--agent-eval-url`.
- Default calls use AICP KOP and KsADK's existing `KSYUN_ACCESS_KEY`, `KSYUN_SECRET_KEY`, `KSYUN_ACCOUNT_ID`, and region configuration.
- `AGENT_EVAL_BASE_URL` remains an advanced direct-HTTP override for local development, Mock tests, and private deployments. It is neither required global configuration nor normal user documentation.
- CLI and Studio construct the same client, so both choose the same default or explicit override.

## Transport Resolution

1. A non-empty `AGENT_EVAL_BASE_URL` selects direct HTTP. KOP signing is never sent to that user-controlled URL.
2. Without an override, KsADK resolves the AICP endpoint using `resolve_aicp_connection()` and calls the required KOP Action.
3. `KSYUN_REGION=pre-online` remains the logical routing signal. Signing uses the standard AICP region, avoiding a synthetic pre-release region in the AWS V4 credential scope.

The client maps its existing operations without changing request payloads:

| Operation | KOP Action |
| --- | --- |
| publish snapshot | `PublishEvaluationSetSnapshot` |
| read fixed version | `DescribeEvaluationSet` |
| list catalog | `ListEvaluationSet` |

## Authentication Boundary

The KOP default authenticates the caller through AK/SK and provides verified account context to agent-eval. No agent-eval-specific API key is added.

Direct HTTP retains compatibility behavior only: `AGENT_EVAL_ACCOUNT_ID` falls back to `KSYUN_ACCOUNT_ID`, and `AGENT_EVAL_API_TOKEN` may provide a bearer token. That mode is opt-in because an application-provided account header is not a trustworthy public identity boundary.

## Failure Behavior

- KOP transport errors are mapped into the existing `AgentEvalCloudClientError` surface with the Action name.
- A direct endpoint failure preserves the existing HTTP error behavior.
- Local EvalSet evaluation never constructs or contacts the cloud client.
- KOP Action registration failure remains visible as its gateway error; the client must not fall back silently to direct HTTP.

## Verification

1. A client without `AGENT_EVAL_BASE_URL` uses a KOP client and passes the correct Action and payload.
2. A client with `AGENT_EVAL_BASE_URL` keeps direct HTTP and never calls KOP.
3. Existing direct HTTP mock tests, immutable pull, binding, and report tests remain green.
4. CLI help contains no `--agent-eval-url` input.
5. Studio constructs its default cloud client even when no direct URL exists.
6. Pre-release verification uses an AK/SK-signed KOP `ListEvaluationSet`, then the full `push -> pull -> eval -> report` path after the new Publish Action is registered.

## Delivery Boundary

This completes the KsADK side of phase-one KOP integration. KOP registration of `PublishEvaluationSetSnapshot` is an external release prerequisite. Cloud Experiment, report/evidence upload, and direct public Internet access remain out of scope.
