# Cloud Evaluation Closed-Loop Design

**Goal:** Run an evaluation against an immutable cloud Dataset version and preserve the remote source reference in the local evaluation report.

## Scope

This iteration implements the KsADK side of the minimum closed loop: preview/publish an EvalSet, list or read a fixed remote Dataset version, execute it through the existing Local Source/A2A/Studio Build adapters, and persist a report that identifies the exact remote Dataset version. Cloud-side Experiment creation and evidence upload remain protocol boundaries until the agent-eval service contract is available.

## Design

- `CloudDatasetRef` identifies `provider`, `projectId`, `datasetId`, `version`, `schemaHash`, and `contentDigest`.
- `EvaluationRequest` accepts either a local `EvalSetVersion` or a resolved remote snapshot, while `EvalRunSpec` always stores the resolved `CloudDatasetRef` when present.
- `CloudDatasetClient` gains catalog and fixed-version read operations. The service validates the returned digest, schema hash, and row count before converting rows through the existing lossless converter.
- CLI commands use the shared `CloudEvalSetService`; remote execution resolves the snapshot once before invoking the existing executor.
- Studio exposes catalog/read operations through the existing evaluation API surface and keeps the existing execution operation unchanged.

## Safety

No target adapter reads cloud data directly. No cloud `current` version is resolved implicitly. A stale or mismatched snapshot fails before target execution, and local reports remain the source of truth for local execution.

## Acceptance

1. A fixed `datasetId + version` round-trips to an equivalent `EvalSetVersion`.
2. Remote execution reports contain the remote reference and the existing TargetSnapshot.
3. Digest/schema/row-count mismatches fail before execution.
4. Existing local, A2A, Studio Build, and Cloud-0/1 tests remain green.
