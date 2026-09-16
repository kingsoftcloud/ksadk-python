# Skill Evaluation Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` and `superpowers:test-driven-development` task-by-task. Work on branch `feat/skill-observability` in an isolated worktree.

**Goal:** Make Skill discovery, loading, execution, cleanup, artifact, and result-consumption facts evaluable by emitting typed KsADK `SkillEvent` records and projecting them through the existing OTLP pipeline.

**Architecture:** Keep `SkillEvent v1` separate from the frozen `RuntimeEvent v1`/SSE/Studio replay contract. An internal KsADK event sink validates correlation and redacts data. Sandbox runtimes return JSONL event envelopes; the outer `execute_skills` path rebuilds OTEL child spans beneath the existing tool span. Existing generic OTLP/Langfuse and Cloud Monitor exporters receive the additive spans unchanged.

**Tech Stack:** Python dataclasses, existing KsADK Skill Runtime, OpenTelemetry, existing OTLP HTTP exporters, pytest via `uv run`.

---

## Fixed Scope And Boundaries

- This plan does not change EvalSmith, `RuntimeEvent v1`, SSE, Studio replay, or the existing exporter setup contract.
- This plan does not give Sandbox a Collector endpoint, `traceparent`, or OTLP credentials.
- `SkillRef` remains package identity: `skill_id`, `version_id`, `version`, `name`, and `content_hash`. Skill Space, owner reference, authorization, and candidate snapshot belong to an immutable `SkillBinding`.
- Legacy `execute_skills` callers retain their arguments and result behavior. They can emit loading/execution facts, but missing binding/candidate data makes selection evaluation `not_evaluable`, never a failure.
- Event/OTEL projection failures are observational diagnostics and must not change workflow success, failure, timeout, or cleanup behavior.

## Data Contract

Create `ksadk/skills/events.py` with the following typed contract:

```python
@dataclass(frozen=True)
class SkillBinding:
    binding_snapshot_id: str
    candidates: tuple[BoundSkillRef, ...]

@dataclass(frozen=True)
class SkillExecutionContext:
    run_id: str = ""
    trace_id: str = ""
    binding: SkillBinding | None = None
    decision_id: str = ""
    selected_skill_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class SkillEvent:
    schema_version: int
    event_id: str
    event_type: str
    status: str
    started_at: float
    ended_at: float | None
    skill_ref: SkillRef | None
    skill_invocation_id: str
    runtime_id: str
    attributes: Mapping[str, JsonValue]
    error_code: str = ""
    error_category: str = ""
```

`BoundSkillRef` contains a `SkillRef` plus `space_id`; it is the only candidate representation accepted by the new request-scoped binding path. Define `SandboxSkillEventEnvelope` as the serializable subset of `SkillEvent` returned by the sandbox, plus `runtime_id`. Its parser rejects unknown schema versions, missing invocation IDs for load-and-later events, unknown event types, and a `SkillRef` that does not match the outer invocation.

Use these event names:

| Boundary | Events |
|---|---|
| Candidate and selection | `skill.candidates.resolved`, `skill.selection.completed`, `skill.selection.skipped` |
| Package and load | `skill.package.cache_hit`, `skill.package.downloaded`, `skill.package.hash_verified`, `skill.package.extracted`, `skill.manifest.parsed`, `skill.load.started`, `skill.load.completed`, `skill.load.failed` |
| Sandbox and execution | `sandbox.session.created`, `sandbox.session.cleaned_up`, `sandbox.session.cleanup_failed`, `skill.execution.started`, `skill.execution.completed`, `skill.execution.failed` |
| Output | `skill.artifact.created`, `skill.result.consumed`, `sandbox.envelope.rejected` |

All events include their timestamps, status, `trace_id` when present, `run_id` when present, binding snapshot ID when present, decision ID when present, complete `SkillRef` when the event is skill-specific, invocation ID from `skill.load.started` onward, and runtime ID for Sandbox-originated facts. Raw prompts, stdout/stderr, download URLs, credentials, filesystem paths, and artifact contents are never event attributes. Artifacts use only a controlled reference, hash, MIME type, size, and availability state.

## Task 1: Add Contract Tests And Models

**Files:**
- Create: `ksadk/skills/events.py`
- Modify: `ksadk/skills/__init__.py`
- Test: `tests/skills/test_events.py`

- [ ] Write failing tests for event round trips, immutable candidate bindings, required correlation, redaction, and invalid envelope rejection.
- [ ] Implement the typed models and a concrete `SkillEventSink` that normalizes timestamps, validates correlation, redacts attributes, and returns accepted/rejected events.
- [ ] Make `not_evaluable` an absence-of-evidence outcome: do not manufacture candidate or selection events for legacy calls without a binding.
- [ ] Run `uv run pytest tests/skills/test_events.py -q` and require a clean pass.

## Task 2: Instrument Package, Loader, And Executor Boundaries

**Files:**
- Modify: `ksadk/skills/package_store.py`
- Modify: `ksadk/skills/loader.py`
- Modify: `ksadk/skills/runtime/agent.py`
- Modify: `ksadk/skills/runtime/executor.py`
- Test: `tests/skills/test_runtime_agent.py`

- [ ] Write failing tests for package cache hit/miss, digest validation, safe extraction, manifest parse failure, load lifecycle, execution lifecycle, and artifact facts.
- [ ] Thread the internal sink and `SkillExecutionContext` through package handling, loader, and executor without adding global mutable state.
- [ ] Assign one generated `skill_invocation_id` per selected Skill before `skill.load.started`; retain it through all package/load/execution/artifact/cleanup events.
- [ ] Emit `skill.execution.failed` with a stable error category for non-zero exits and timeouts; preserve the existing `WorkflowExecution` status and result payload.
- [ ] Run `uv run pytest tests/skills/test_runtime_agent.py -q` and require a clean pass.

## Task 3: Return Sandbox Envelopes From Both Backends

**Files:**
- Modify: `ksadk/skills/runtime/base.py`
- Modify: `ksadk/skills/runtime/backends/local.py`
- Modify: `ksadk/skills/runtime/backends/e2b.py`
- Modify: `ksadk/skills/runtime/agent.py`
- Test: `tests/skills/test_runtime.py`

- [ ] Write failing local and fake-E2B tests for valid JSONL envelope recovery, malformed JSONL, correlation mismatch, timeout, and cleanup failure.
- [ ] Add `skill_events: list[SkillEvent] = field(default_factory=list)` to `SkillRuntimeResult`; `to_dict()` includes `skill_events` only when non-empty.
- [ ] Allocate a backend-controlled JSONL path for each request and pass only that path to the runtime agent via an internal environment variable.
- [ ] Have the runtime agent append redacted envelopes, and have each backend read and parse the file after command completion, including failed commands where the file exists.
- [ ] The outer sink accepts only validated envelopes and emits `sandbox.envelope.rejected` for malformed or mismatched records; the original result remains unchanged.
- [ ] Run `uv run pytest tests/skills/test_runtime.py -q` and require a clean pass.

## Task 4: Preserve Legacy Toolsets And Add Request-Scoped Context

**Files:**
- Modify: `ksadk/skills/tool_defs.py`
- Modify: `ksadk/toolsets/skills.py`
- Modify: `ksadk/runners/adk_runner.py`
- Test: `tests/skills/test_loader_and_tools.py`
- Test: `tests/skills/test_adk_runner_skill_runtime.py`

- [ ] Write failing tests showing that existing `build_execute_skills_tool` calls retain their arguments, output, Space behavior, and session behavior when no context is supplied.
- [ ] Add an internal-only optional `SkillExecutionContext` argument to the tool builder; do not expose a user-supplied context in the public tool schema.
- [ ] For the new Agent Runtime path, capture a trusted request-scoped binding, candidate snapshot, decision ID, and selected Skill IDs in the generated tool closure.
- [ ] Emit candidate/selection facts only from the trusted new path. Continue using the legacy ADK manifest injection path unchanged when request binding is unavailable.
- [ ] Expose accepted event diagnostics as an additive tool result only when events exist; do not alter legacy empty-result snapshots.
- [ ] Run `uv run pytest tests/skills/test_loader_and_tools.py tests/skills/test_adk_runner_skill_runtime.py -q` and require a clean pass.

## Task 5: Rebuild OTEL Spans Outside Sandbox

**Files:**
- Create: `ksadk/skills/observability.py`
- Modify: `ksadk/skills/tool_defs.py`
- Test: `tests/skills/test_skill_event_tracing.py`
- Test: `tests/test_tracing_setup_otlp.py`
- Test: `tests/test_tracing_cloud_monitor_e2e.py`

- [ ] Write failing tests for parentage under the active `execute_skills` span, status mapping, attribute redaction, and preservation of generic/Cloud Monitor exporter behavior.
- [ ] Rebuild duration-bearing events as child spans: package, hash verification, extraction, load, sandbox session, execution, and cleanup.
- [ ] Add instantaneous candidate, selection, manifest, artifact, result-consumption, and rejected-envelope facts as span events on the current `execute_skills` span.
- [ ] Use only additive `ksadk.skill.*` attribute keys. Map `failed`, `timed_out`, and `cleanup_failed` to OTEL error status; leave `skipped` non-error with a reason attribute.
- [ ] Catch OTEL import, span creation, and exporter errors inside the projection path and return diagnostics rather than affecting workflow execution.
- [ ] Run `uv run pytest tests/skills/test_skill_event_tracing.py tests/test_tracing_setup_otlp.py tests/test_tracing_cloud_monitor_e2e.py -q` and require a clean pass.

## Task 6: Document Cross-System Contract And Verify The Integrated Slice

**Files:**
- Create: `docs/internal/skill-runtime-evaluation-observability-design.md`
- Modify: `docs/internal/ksadk-skills-analysis-and-design.md`

- [ ] Document that Agent Runtime owns request binding, candidate snapshot, selection receipt, and result-consumption step linkage.
- [ ] Document that Skill Service provides authorized immutable Skill/Version/digest/Space facts and that download URLs remain KsADK-internal.
- [ ] Document that Sandbox Service owns session, runtime ID, execution/kill state, and controlled event-file retrieval, but not `SKILL.md` semantics or OTLP export.
- [ ] Document that EvalSmith is out of scope and later consumes the resulting OTEL contract; it must report missing candidate/binding/invocation evidence as `not_evaluable`.
- [ ] Run the complete focused suite:

```bash
uv run pytest \
  tests/skills/test_events.py \
  tests/skills/test_runtime.py \
  tests/skills/test_runtime_agent.py \
  tests/skills/test_loader_and_tools.py \
  tests/skills/test_adk_runner_skill_runtime.py \
  tests/skills/test_skill_event_tracing.py \
  tests/test_tracing_setup_otlp.py \
  tests/test_tracing_cloud_monitor_e2e.py -q
```

- [ ] Run a targeted secret scan over changed Skill Runtime files and documentation. Do not run real E2B E2E without a registered template, authorized binding, pre-production Skill Service, and non-sensitive OTLP configuration; record any missing prerequisite explicitly.
