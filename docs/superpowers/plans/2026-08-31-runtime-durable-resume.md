# Runtime Durable Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ADK and LangGraph checkpoint recovery work through RuntimeExecutor after a process restart, while guaranteeing terminal session state for every detached ResumeRun failure.

**Architecture:** ADKRunner and LangGraphRunner own durable handle attachment because only the framework runner can validate its persistence backend and native resume reference. All ResumeRun transport modes drive the same RuntimeExecutor conversation stream; detached transports always receive session identity so their existing fallback can persist failure or cancellation.

**Tech Stack:** Python 3.10+, FastAPI, asyncio, Pydantic Runtime v2 models, Google ADK, LangGraph, pytest, httpx, uv.

**Spec:** `docs/superpowers/specs/2026-08-31-runtime-durable-resume-design.md`

## Global Constraints

- Do not change public API field names.
- Do not add durable restore to BaseRunner, Harness, memory, local, SQLite, or degraded persistence backends.
- Preserve fail-closed checkpoint ownership and persistence checks.
- Do not change versions, CHANGELOG release entries, or publish artifacts.
- Preserve the untracked root `agentengine.yaml`.

---

### Task 1: Framework-Owned Durable Handle Attachment

**Files:**
- Modify: `ksadk/runners/adk_runner.py`
- Modify: `ksadk/runners/langgraph_runner.py`
- Test: `tests/runtime/test_capability_matrix.py`
- Test: `tests/runtime/test_conversation_execution.py`
- Test: `tests/test_runner.py`
- Test: `tests/test_langgraph_runner_resume.py`

**Interfaces:**
- Consumes: `RunHandle(runtime_type, run_id, session_id, native_ref)` from `ksadk.runtime.adapter`.
- Produces: `ADKRunner.attach_runtime_handle(handle) -> bool` and `LangGraphRunner.attach_runtime_handle(handle) -> bool`, both async-compatible through `RunnerRuntimeAdapter.attach()`.

- [ ] **Step 1: Write failing attach contract tests**

Add literal behavior cases proving built-in ADK/LangGraph runners expose the seam only when their own shared durable capability is ready, reject mismatched runtime types and incomplete native refs, and do not accept SQLite/memory/degraded capability.

```python
handle = RunHandle(
    run_id="run-1",
    session_id="session-1",
    runtime_type="langgraph",
    native_ref={
        "checkpoint_id": "checkpoint-1",
        "known_checkpoint_ids": ["checkpoint-1"],
        "thread_id": "session-1:run-1",
    },
)
assert await runner.attach_runtime_handle(handle) is True
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest -q tests/runtime/test_capability_matrix.py tests/runtime/test_conversation_execution.py tests/test_runner.py tests/test_langgraph_runner_resume.py
```

Expected: new tests fail because ADKRunner and LangGraphRunner do not define `attach_runtime_handle` and their adapter matrix reports attach unavailable.

- [ ] **Step 3: Implement minimal runner-owned attach**

For each framework, prepare/refresh runtime capability state, validate framework identity and required native ref, then require `Supported`, `Durable`, and `SharedAcrossPods` from `describe_checkpoint_capability()`. LangGraph additionally confirms the referenced checkpoint through its graph/checkpointer state lookup; ADK defers invocation existence to ADK `run_async(invocation_id=...)` but rejects a missing invocation reference.

```python
async def attach_runtime_handle(self, handle: RunHandle) -> bool:
    if handle.runtime_type != "adk":
        return False
    await self.prepare_runtime_capabilities()
    capability = self.describe_checkpoint_capability()
    if not all(capability.get(key) for key in ("Supported", "Durable", "SharedAcrossPods")):
        return False
    return bool(_adk_invocation_id(handle.native_ref))
```

- [ ] **Step 4: Run the attach tests and verify GREEN**

Run the Task 1 pytest command and require all cases to pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add ksadk/runners/adk_runner.py ksadk/runners/langgraph_runner.py tests/runtime/test_capability_matrix.py tests/runtime/test_conversation_execution.py tests/test_runner.py tests/test_langgraph_runner_resume.py
git commit -m "fix(runtime): attach durable framework handles"
```

### Task 2: ResumeRun Transport Unification and Terminal Fallback

**Files:**
- Modify: `ksadk/server/routes/control.py`
- Test: `tests/test_server_session_app.py`
- Test: `tests/server/test_runtime_executor_routes.py`

**Interfaces:**
- Consumes: `stream_runtime_responses_conversation_turn(executor, launch_context, ..., resume_input)`.
- Produces: both `Stream=true` and `Background=true` register a detached RuntimeExecutor stream with `session_id`, `invocation_id`, `resume_key`, `run_mode`, and `run_trigger`.

- [ ] **Step 1: Write failing route behavior tests**

Add one test where a detached Stream resume raises during attach and assert the persisted last status is `failed`, plus one Background resume test that consumes the runtime stream and completes without accessing a legacy runner variable.

```python
statuses = [
    event.metadata.get("status")
    for event in await service.get_events("session-1")
    if event.event_type == "run_status" and event.invocation_id == "resume-1"
]
assert statuses == ["resuming", "failed"]
```

- [ ] **Step 2: Run route tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_server_session_app.py -k 'resume_run_action and (stream or background)'
uv run pytest -q tests/server/test_runtime_executor_routes.py -k resume
```

Expected: Stream failure remains `resuming`; Background raises `NameError: active_runner is not defined`.

- [ ] **Step 3: Implement the minimal transport fix**

Replace Background's legacy `stream_responses_conversation_turn(runner=active_runner, ...)` source with `stream_runtime_responses_conversation_turn(executor=executor, launch_context=launch_context, ...)`. Pass `session_id=request.SessionId` into both detached response constructors and preserve the existing resume-key cleanup callback.

```python
return deps.detached_streaming_response(
    stream_runtime_responses_conversation_turn(
        executor=executor,
        launch_context=launch_context,
        ...,
    ),
    invocation_id=resume_invocation_id,
    session_id=request.SessionId,
    resume_key=resume_key,
    run_mode=RUN_MODE_BACKGROUND,
    run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
)
```

- [ ] **Step 4: Run route tests and verify GREEN**

Run both Task 2 pytest commands and require terminal events to be present for success and failure.

- [ ] **Step 5: Commit Task 2**

```bash
git add ksadk/server/routes/control.py tests/test_server_session_app.py tests/server/test_runtime_executor_routes.py
git commit -m "fix(server): terminate detached resume failures"
```

### Task 3: Cross-Process ADK and LangGraph Resume Regression Matrix

**Files:**
- Modify: `tests/runtime/test_conversation_execution.py`
- Modify: `tests/server/test_runtime_executor_routes.py`
- Modify: `tests/test_server_session_app.py`

**Interfaces:**
- Consumes: a fresh `RuntimeExecutor` with no owned in-memory handle plus persisted ResumeRun framework refs.
- Produces: regression coverage for same-process and post-restart resume across synchronous, Stream, and Background modes.

- [ ] **Step 1: Add fresh-executor regression tests**

Build runner fixtures that inherit the real framework attach behavior while replacing only external database calls. Assert the runner receives the exact native resume input after attach:

```python
assert runner.calls[-1]["checkpoint_resume"] is True
assert runner.calls[-1]["framework_ref"] == {
    "langgraph": {
        "checkpoint_id": "checkpoint-1",
        "thread_id": "session-1:run-1",
    }
}
```

Add equivalent ADK assertions for `framework_ref.adk.invocation_id` and negative cases for non-shared persistence.

- [ ] **Step 2: Run the matrix and verify it detects any remaining gap**

```bash
uv run pytest -q tests/runtime/test_conversation_execution.py tests/server/test_runtime_executor_routes.py tests/test_server_session_app.py -k 'checkpoint_resume or resume_run'
```

Expected before any additional implementation: failures identify missing propagation or validation rather than test fixture errors.

- [ ] **Step 3: Make only the propagation/validation changes required by the matrix**

Keep framework-specific native ref normalization in `_persisted_resume_native_ref()` and `_resume_target()`; do not bypass `executor.attach()` or relax checkpoint ownership.

- [ ] **Step 4: Run the matrix and verify GREEN**

Run the Task 3 command until all selected tests pass without warnings or pending detached tasks.

- [ ] **Step 5: Commit Task 3**

```bash
git add ksadk/runtime/conversation_execution.py tests/runtime/test_conversation_execution.py tests/server/test_runtime_executor_routes.py tests/test_server_session_app.py
git commit -m "test(runtime): cover restarted checkpoint resume"
```

### Task 4: Verification and Real Example Regression

**Files:**
- No production files expected.
- Preserve: `agentengine.yaml`.

**Interfaces:**
- Consumes: completed Task 1-3 implementation.
- Produces: fresh automated verification plus real PostgreSQL smoke evidence.

- [ ] **Step 1: Run focused framework and persistence suites**

```bash
uv run pytest -q tests/test_persistence_capability.py tests/test_persistence_capability_coordinator.py
uv run pytest -q tests/test_runner.py tests/test_langgraph_runner_resume.py
uv run pytest -q tests/runtime/test_capability_matrix.py tests/runtime/test_conversation_execution.py
```

- [ ] **Step 2: Run server/session suites**

```bash
uv run pytest -q tests/server/test_ui_bootstrap_agui.py tests/server/test_runtime_executor_routes.py
uv run pytest -q tests/test_server_session_app.py
```

- [ ] **Step 3: Run the real deepsearch-langgraph restart smoke**

Start `examples/deepsearch-langgraph` with its configured environment, resume a known PostgreSQL checkpoint through `Stream=true`, and verify a new invocation progresses beyond `run_resume` to a terminal `completed` or an honest external-service `failed` event. Restart the server before the resume so no in-memory handle can satisfy attach.

- [ ] **Step 4: Run ADK PostgreSQL smoke when prerequisites exist**

Use configured shared ADK session persistence and resumability. If no usable ADK DSN/session fixture exists, record the exact missing prerequisite and rely on the real adapter/in-memory-boundary integration tests rather than claiming an E2E pass.

- [ ] **Step 5: Inspect changes and secrets**

```bash
git diff --check
git status --short
git diff --name-only HEAD~3..HEAD
rg -n '(postgres(ql)?://[^[:space:]]+:[^@[:space:]]+@|api[_-]?key[[:space:]]*=[[:space:]]*[^<])' ksadk tests docs/superpowers
```

Do not print `.env` values or database credentials.

- [ ] **Step 6: Commit any verification-only test correction**

Only if verification required a test correction, commit the scoped files with:

```bash
git commit -m "test(runtime): verify durable resume recovery"
```
