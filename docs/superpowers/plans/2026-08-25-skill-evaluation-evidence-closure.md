# Skill Evaluation Evidence Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` and `superpowers:test-driven-development` task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining trusted-correlation, timing, result-consumption, and cross-system evidence gaps so that a downstream evaluator can distinguish `pass`/`fail` from `not_evaluable` and `not_applicable`.

**Architecture:** Keep KsADK as the Skill Runtime evidence producer. Agent Runtime supplies immutable request binding and consumption receipts; KsADK creates an outer invocation plan, validates Sandbox JSONL against that plan, and projects accepted facts through existing OTLP. EvalSmith remains out of scope: it only consumes the contract after these phases are delivered.

**Tech Stack:** Python dataclasses, KsADK Skill Runtime, OpenTelemetry, pytest through `uv run`; Agent Runtime and Sandbox Service contract changes follow their owning repositories.

---

## Scope And Non-goals

- This plan deliberately does **not** improve execution adaptability. The Runtime continues to execute only its existing supported workflows; it must not infer arbitrary shell commands from `SKILL.md`.
- Do not modify `RuntimeEvent v1`, SSE, Studio replay, existing exporter configuration, or send OTLP credentials/trace context into Sandbox.
- Do not implement EvalSmith evaluators, LLM judges, business-result scoring, or a generic `SKILL.md` interpreter here.
- Preserve legacy ADK injection: missing request binding means selection/consumption dimensions are `not_evaluable`, not failed.

## Deliverable Sequence

| Phase | Independently usable outcome | Owner |
|---|---|---|
| P0: Trusted invocation | Exact selected Skill/version/invocation IDs are fixed outside Sandbox and every returned envelope is validated against them. | KsADK + Agent Runtime |
| P1: Complete runtime facts | Per-step package timings and an Agent-owned result-consumption receipt make execution health and use of output evaluable. | KsADK + Agent Runtime |
| P2: Evidence readiness | Contract fixtures, integration checks, updated E2E runbook, and one authorized pre-production slice prove evidence is consumable without changing scoring. | KsADK + Skill Service + Sandbox Service |

## Required Contract

P0 introduces internal-only data supplied by Agent Runtime to the existing `build_execute_skills_tool` closure. It is never a model tool argument:

```python
@dataclass(frozen=True)
class SkillInvocationPlan:
    binding_snapshot_id: str
    decision_id: str
    entries: tuple[PlannedSkillInvocation, ...]

@dataclass(frozen=True)
class PlannedSkillInvocation:
    skill_ref: SkillRef
    skill_invocation_id: str
```

`entries` is derived only from a request-scoped `SkillBinding` and the Agent Runtime selection receipt. KsADK rejects a selected ID absent from the binding before creating Sandbox work. The outer backend overwrites `run_id`, `trace_id`, binding, decision, invocation, and `runtime_id`; Sandbox is only an untrusted source of event type, timestamps, allowed attributes, and status.

P1 adds `agent_step_id` and `selection_receipt_id` as trusted `SkillExecutionContext` fields and allowlisted OTEL attributes. `skill.result.consumed` is emitted only when Agent Runtime records the concrete step that consumed a returned `skill_invocation_id`; KsADK must never infer it from the final response.

## Task 0: Locate The Request-path Agent Runtime Owner (P0 Gate)

**Repositories:**
- Inspect: `/Users/hush/Code/agentengine-server` when available, then the deployed Agent Runtime source checkout named by its build artifact.
- Do not modify: `/Users/hush/Code/bigdata-platform/agent-runtime-service` unless it proves to own model-request assembly and tool-result step recording.

- [ ] **Step 1: Prove the request-path owner from source and deployment metadata.**

Search for the call path that creates `build_execute_skills_tool`, invokes the model, and persists tool-call/step results:

```bash
rg -n "build_execute_skills_tool|execute_skills|tool.*result|skill.*binding" \
  /Users/hush/Code/agentengine-server /Users/hush/Code/bigdata-platform
```

Expected: one service/repository contains both request-scoped authorization candidates and the durable agent-step record. Record its repository, source module, request DTO, persisted record, deployed image, and test command in the implementation issue before Task 2.

- [ ] **Step 2: Stop if the owner cannot be proven.**

Do not add a binding field to `agent-runtime-service` merely because its name matches. Complete the KsADK-only Tasks 1, 3, and 4 if desired, but leave Agent Runtime binding and `skill.result.consumed` pending until the owner is identified.

## Task 1: Freeze The Cross-system Receipt Contract (P0)

**Files:**
- Modify: `docs/internal/skill-runtime-evaluation-observability-design.md`
- Modify: `docs/internal/ksadk-skills-analysis-and-design.md`
- Create: `docs/internal/contracts/skill-execution-evidence-v1.json`
- Test: `tests/skills/test_events.py`

- [ ] **Step 1: Write failing contract tests.**

```python
def test_invocation_plan_rejects_selected_skill_outside_binding() -> None:
    binding = SkillBinding("binding-1", (_bound("skill-a"),))
    with pytest.raises(ValueError, match="not present in SkillBinding"):
        build_skill_invocation_plan(binding, selected_skill_ids=("skill-b",))
```

- [ ] **Step 2: Run the focused test.**

Run: `uv run pytest tests/skills/test_events.py::test_invocation_plan_rejects_selected_skill_outside_binding -q`

Expected: FAIL because `build_skill_invocation_plan` does not exist.

- [ ] **Step 3: Define the additive KsADK contract.**

Create `SkillInvocationPlan`, `PlannedSkillInvocation`, and `build_skill_invocation_plan()` in `ksadk/skills/events.py`. Generate one UUID-based invocation ID per selected immutable `SkillRef`; reject empty binding IDs, duplicate selected IDs, and any selection outside the binding. Extend `SkillExecutionContext` only with `agent_step_id` and `selection_receipt_id`, both defaulting to `""` for compatibility.

- [ ] **Step 4: Add the owner-facing JSON fixture.**

Write `docs/internal/contracts/skill-execution-evidence-v1.json` with one valid request: binding snapshot, selection receipt, selected complete `SkillRef`, generated invocation ID, and a result-consumption receipt. Use only synthetic IDs. Document fields, source of truth, mutability, and reject semantics in both internal design documents.

- [ ] **Step 5: Verify contract behavior.**

Run: `uv run pytest tests/skills/test_events.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the contract slice.**

```bash
git add ksadk/skills/events.py tests/skills/test_events.py docs/internal
git commit -m "feat(skills): define trusted invocation evidence"
```

## Task 2: Bind Agent Runtime Requests To The Existing Tool Closure (P0)

**Files:**
- Modify: `ksadk/skills/tool_defs.py`
- Modify: `ksadk/skills/runtime/base.py`
- Test: `tests/skills/test_loader_and_tools.py`
- Test: `tests/skills/test_adk_runner_skill_runtime.py`
- Cross-repository implementation owner: Agent Runtime request assembly module, discovered and modified in its owning checkout during execution.

- [ ] **Step 1: Write the failing KsADK integration test.**

```python
def test_bound_tool_passes_only_planned_invocations_to_backend() -> None:
    tool = build_execute_skills_tool(backend=backend, execution_context=_context())
    tool("run", skill_names=["skill-a"])
    assert backend.request.invocation_plan.entries[0].skill_ref.skill_id == "skill-a"
```

- [ ] **Step 2: Run it.**

Run: `uv run pytest tests/skills/test_loader_and_tools.py::test_bound_tool_passes_only_planned_invocations_to_backend -q`

Expected: FAIL because backend requests do not carry an invocation plan.

- [ ] **Step 3: Thread the plan through KsADK.**

Add optional `invocation_plan: SkillInvocationPlan | None = None` to the internal `SkillRuntimeBackend.run_workflow()` protocol and concrete backends. `build_execute_skills_tool` creates it only when a binding and selected Skills are available, passes it to the backend, and retains legacy call behavior when context is absent. Do not add parameters to the model-visible `execute_skills` schema.

- [ ] **Step 4: Implement the Agent Runtime owner task.**

At request creation, Agent Runtime must persist an immutable `SkillBinding` obtained from authorized Skill Service candidates; after selection it persists `selection_receipt_id`, `decision_id`, selected `SkillRef` values, `run_id`, `trace_id`, and `agent_step_id`. It creates the KsADK tool closure with this trusted context. Legacy injection remains unchanged. The owner must add a contract test that a model-supplied identifier cannot replace the persisted binding or receipt.

- [ ] **Step 5: Verify compatibility and request binding.**

Run: `uv run pytest tests/skills/test_loader_and_tools.py tests/skills/test_adk_runner_skill_runtime.py -q`

Expected: PASS, including legacy calls with no `skill_events` result field.

- [ ] **Step 6: Commit only the KsADK slice.**

```bash
git add ksadk/skills/tool_defs.py ksadk/skills/runtime/base.py tests/skills
git commit -m "feat(skills): bind trusted skill invocations"
```

## Task 3: Validate Sandbox Envelopes Against The Outer Plan (P0)

**Files:**
- Modify: `ksadk/skills/events.py`
- Modify: `ksadk/skills/runtime/agent.py`
- Modify: `ksadk/skills/runtime/backends/local.py`
- Modify: `ksadk/skills/runtime/backends/e2b.py`
- Test: `tests/skills/test_events.py`
- Test: `tests/skills/test_runtime.py`
- Test: `tests/skills/test_runtime_agent.py`

- [ ] **Step 1: Write failing local and fake-E2B tests.**

```python
def test_backend_rejects_envelope_not_in_outer_invocation_plan() -> None:
    result = backend.run_workflow("run", skill_space_ids=[], session_id="s", invocation_plan=_plan())
    assert "sandbox.envelope.rejected" in _event_types(result)
    assert all(event.skill_ref != _unplanned_ref() for event in result.skill_events)
```

- [ ] **Step 2: Run the focused tests.**

Run: `uv run pytest tests/skills/test_runtime.py tests/skills/test_runtime_agent.py -q`

Expected: FAIL because JSONL parsing accepts events without the outer plan.

- [ ] **Step 3: Make the invocation plan a controlled runtime input.**

Serialize the selected complete refs and invocation IDs in the backend-created request file, not an environment variable. The runtime agent loads only that file, emits events using those IDs, and does not accept an invocation ID from workflow prompt, `skill_names`, or Skill package content.

- [ ] **Step 4: Enforce plan validation at recovery.**

Change JSONL parsing to accept `expected_invocations: Mapping[str, SkillRef]` and reject each load/execution/artifact event whose `(skill_invocation_id, SkillRef)` pair is absent or mismatched. Backend code overwrites any Sandbox-originated outer correlation and `runtime_id`. Keep malformed JSON, unknown schema, unknown event type, missing required invocation, and duplicate terminal event handling as `sandbox.envelope.rejected` with redacted diagnostics.

- [ ] **Step 5: Verify local and E2B fakes.**

Run: `uv run pytest tests/skills/test_events.py tests/skills/test_runtime.py tests/skills/test_runtime_agent.py -q`

Expected: PASS; ensure timeout and cleanup return semantics do not change.

- [ ] **Step 6: Commit the envelope slice.**

```bash
git add ksadk/skills/events.py ksadk/skills/runtime tests/skills
git commit -m "fix(skills): validate sandbox event envelopes"
```

## Task 4: Produce Accurate Package And Lifecycle Durations (P1)

**Files:**
- Modify: `ksadk/skills/package_store.py`
- Modify: `ksadk/skills/loader.py`
- Modify: `ksadk/skills/observability.py`
- Test: `tests/skills/test_package_store.py`
- Test: `tests/skills/test_runtime_agent.py`
- Test: `tests/skills/test_skill_event_tracing.py`

- [ ] **Step 1: Write timing tests around the actual boundaries.**

```python
def test_hash_and_extract_emit_distinct_completed_intervals(monkeypatch) -> None:
    result = store.store_archive(_archive(), expected_hash=_hash(), event_sink=sink)
    assert _event(sink, "skill.package.hash_verified").ended_at is not None
    assert _event(sink, "skill.package.extracted").started_at >= _event(sink, "skill.package.hash_verified").ended_at
```

- [ ] **Step 2: Run the focused tests.**

Run: `uv run pytest tests/skills/test_package_store.py tests/skills/test_runtime_agent.py -q`

Expected: FAIL because hash and extraction use coarse store-level timing.

- [ ] **Step 3: Instrument the source-of-truth operations.**

Emit paired start/completion or failure events immediately around `PackageStore._verify_hash()` and `PackageStore._safe_extract()`. Keep cache, download, manifest, load, execution, session, and cleanup timings attached to their actual producer; do not derive child duration from the whole workflow.

- [ ] **Step 4: Preserve OTEL semantics.**

`project_skill_events()` creates a child span only for paired lifecycle events. A failed terminal event has OTEL error status; `skipped` is non-error and keeps allowlisted reason. Attribute keys stay under `ksadk.skill.*`.

- [ ] **Step 5: Verify runtime and trace behavior.**

Run: `uv run pytest tests/skills/test_package_store.py tests/skills/test_runtime_agent.py tests/skills/test_skill_event_tracing.py -q`

Expected: PASS with no exporter configuration changes.

- [ ] **Step 6: Commit the timing slice.**

```bash
git add ksadk/skills/package_store.py ksadk/skills/loader.py ksadk/skills/observability.py tests/skills
git commit -m "feat(skills): record package lifecycle durations"
```

## Task 5: Record Agent-owned Result Consumption (P1)

**Files:**
- Modify: `ksadk/skills/events.py`
- Modify: `ksadk/skills/tool_defs.py`
- Test: `tests/skills/test_loader_and_tools.py`
- Cross-repository implementation owner: Agent Runtime tool-result/step recorder, discovered and modified in its owning checkout during execution.

- [ ] **Step 1: Write a failing KsADK receipt test.**

```python
def test_result_consumption_requires_matching_agent_step_and_invocation() -> None:
    event = record_skill_result_consumption(result, context, agent_step_id="step-2")
    assert event.event_type == "skill.result.consumed"
    assert event.skill_invocation_id == _plan().entries[0].skill_invocation_id
```

- [ ] **Step 2: Run it.**

Run: `uv run pytest tests/skills/test_loader_and_tools.py::test_result_consumption_requires_matching_agent_step_and_invocation -q`

Expected: FAIL because the runtime has no explicit consumption receipt API.

- [ ] **Step 3: Add the smallest receipt API.**

Add an internal function that accepts only a `SkillRuntimeResult`, trusted `SkillExecutionContext`, concrete `agent_step_id`, and invocation IDs returned by that result. It emits `skill.result.consumed` only for a completed invocation in the request plan; otherwise it returns a rejected diagnostic and does not alter the workflow result. Never inspect final natural-language text to infer consumption.

- [ ] **Step 4: Implement the Agent Runtime owner task.**

When the orchestrator attaches a tool result to a defined agent step, call the internal receipt API with that persisted step ID. Persist the receipt with `run_id`, binding snapshot, selection receipt, decision, invocation ID, and the resulting step link. Do not emit it merely because the tool returned or the answer completed.

- [ ] **Step 5: Verify no legacy regression.**

Run: `uv run pytest tests/skills/test_loader_and_tools.py tests/skills/test_adk_runner_skill_runtime.py -q`

Expected: PASS; old callers neither receive a fabricated consumption event nor a changed output contract.

- [ ] **Step 6: Commit the KsADK receipt slice.**

```bash
git add ksadk/skills/events.py ksadk/skills/tool_defs.py tests/skills
git commit -m "feat(skills): record result consumption receipts"
```

## Task 6: Establish Evaluation-readiness Fixtures And E2E Evidence (P2)

**Files:**
- Modify: `docs/internal/skill-runtime-e2e.md`
- Modify: `docs/internal/skill-runtime-evaluation-observability-design.md`
- Create: `tests/skills/fixtures/skill_execution_evidence_v1.json`
- Test: `tests/skills/test_skill_event_tracing.py`
- Test: `tests/skills/test_runtime.py`

- [ ] **Step 1: Add a redacted end-to-end evidence fixture.**

The fixture must contain one command-style Skill with complete candidate, selection, package, invocation, Sandbox, artifact, consumption, trace, and correlation fields. Include a second instruction-only Skill with no executable entrypoint and expected `not_applicable` for execution health. Use fake IDs, hashes, output references, and timestamps only.

- [ ] **Step 2: Test the downstream-readiness invariants in KsADK.**

```python
def test_evidence_fixture_has_minimum_keys_for_each_evaluable_dimension() -> None:
    evidence = _load_fixture("skill_execution_evidence_v1.json")
    assert _has_selection_evidence(evidence["command_skill"])
    assert _has_consumption_evidence(evidence["command_skill"])
    assert evidence["instruction_skill"]["execution_health"] == "not_applicable"
```

- [ ] **Step 3: Update the E2E runbook.**

Replace stale `deploy/skill-runtime/agent.py` statements with `ksadk/skills/runtime/agent.py`; add envelope recovery, rejection, `SkillEvent`/OTEL verification, and correlation receipt checks. Remove obsolete real fixture hashes, historical infrastructure details, and all credential-shaped examples from this document.

- [ ] **Step 4: Run the complete KsADK verification slice.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/ksadk-skill-observability-uv-cache uv run pytest \
  tests/skills/test_events.py \
  tests/skills/test_package_store.py \
  tests/skills/test_runtime.py \
  tests/skills/test_runtime_agent.py \
  tests/skills/test_loader_and_tools.py \
  tests/skills/test_adk_runner_skill_runtime.py \
  tests/skills/test_skill_event_tracing.py \
  tests/test_tracing_setup_otlp.py \
  tests/test_tracing_cloud_monitor_e2e.py -q
```

Expected: PASS. Then run `uv run ruff check` on changed Python files, `uv run black --check` on the same files, `git diff --check`, and a targeted secret scan over changed runtime/tests/docs.

- [ ] **Step 5: Run one authorized pre-production E2E.**

Only proceed with a registered non-production template, authorized immutable binding, Skill Service download access, and non-sensitive OTLP endpoint/configuration. Verify: complete matching invocation plan; expected JSONL acceptance; `execute_skills` child span parentage; no `OTEL_*`, `LANGFUSE_*`, or trace context passed into Sandbox; timeout/cleanup remain correct. If a prerequisite is missing, record it and leave P2 E2E pending rather than substituting a simulated pass.

- [ ] **Step 6: Commit documents and fixtures.**

```bash
git add docs/internal tests/skills/fixtures tests/skills
git commit -m "docs(skills): define evaluation evidence readiness"
```

## Evaluation Boundary After This Plan

| Question | Evidence produced by this plan | Evaluation owner and result |
|---|---|---|
| Was the right authorized Skill selected? | binding, selection receipt, complete `SkillRef`, decision ID | EvalSmith deterministic selection evaluator; missing evidence is `not_evaluable` |
| Did an executable Skill load and run reliably? | paired package/load/execution/session events, exit/timeout/cleanup and artifacts | EvalSmith runtime-health evaluator; instruction-only Skill is `not_applicable` |
| Was its output used by the Agent? | matching invocation and Agent Runtime result-consumption receipt | EvalSmith trajectory/conformance evaluator; missing receipt is `not_evaluable` |
| Was business behavior and final outcome correct? | input/output artifact contracts and the consumption link, not stdout alone | EvalSmith fixture assertions and optional domain/LLM judge; never decided by KsADK observability |
| Is a pure instruction Skill high quality? | candidate/selection/consumption plus Agent trajectory and final task evidence | EvalSmith agent-level conformance/outcome evaluation; no standalone Sandbox execution score |

## Exit Criteria

- P0: Every accepted Sandbox event is tied to one immutable outer invocation plan; legacy paths retain prior behavior.
- P1: Hash/extract timings are not coarse workflow approximations, and consumption is explicit rather than inferred.
- P2: Contract fixture and focused test suite pass; pre-production E2E is either recorded as passed with redacted evidence or clearly pending on named prerequisites.
- No implementation starts on generalized `SKILL.md` execution until a separate approved design defines declarative runtime contracts and capability preflight.
