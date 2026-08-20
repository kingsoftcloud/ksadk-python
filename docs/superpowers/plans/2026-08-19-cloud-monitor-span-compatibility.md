# Cloud Monitor Span Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Cloud Monitor-only OTLP resource and span compatibility without changing native Langfuse export data or trace identity.

**Architecture:** Reuse the historical exporter transform boundary. Generic OTLP receives original spans, while the Cloud Monitor delegating exporter clones resources/spans, changes only its `service.name`, copies selected resource attributes as `gen_ai.agentengine.*`, adds missing standard Langfuse Span attributes, and restores root usage rollups.

**Tech Stack:** Python, OpenTelemetry SDK, pytest, OTLP HTTP/protobuf

---

### Task 1: Lock the exporter contract with tests

**Files:**
- Modify: `tests/test_tracing_setup_otlp.py`
- Modify: `tests/test_tracing_cloud_monitor_e2e.py`

- [x] Add a focused unit test that creates root/child spans with resource attributes and asserts Cloud Monitor copies use `service.name=agent_id`, copy the five fields as `gen_ai.agentengine.*`, add standard Langfuse Span attributes with `setdefault`, retain resource fields, and leave original spans unchanged.
- [ ] Extend the dual-export E2E fixture so the generic receiver retains the runtime service name while Cloud Monitor receives the agent ID and copied span attributes with identical trace/span IDs.
- [ ] Run `uv run pytest tests/test_tracing_setup_otlp.py tests/test_tracing_cloud_monitor_e2e.py -q` and confirm the new assertions fail because no Cloud Monitor transform is registered.

### Task 2: Restore and extend the Cloud Monitor transform

**Files:**
- Modify: `ksadk/tracing/setup.py`

- [ ] Restore the optional `span_transform` parameter on `_LoggingSpanExporter` and invoke it immediately before delegate export.
- [ ] Restore the historical span identity/token helpers and `_prepare_cloud_monitor_spans` leaf-token rollup.
- [x] Extend cloning to preserve the original Resource attributes, override only cloned `service.name` from `agentengine.agent_id`, and copy the five selected resource values to `gen_ai.agentengine.*` Span attributes using `setdefault`.
- [x] Populate missing `session.id` and standard Langfuse observation type/provider/model/usage Span attributes from existing OpenInference/GenAI fields without changing source spans.
- [ ] Register `_prepare_cloud_monitor_spans` only on the Cloud Monitor exporter.
- [ ] Run the focused test command and confirm all tests pass.

### Task 3: Verify isolation and code quality

**Files:**
- Verify: `ksadk/tracing/setup.py`
- Verify: `tests/test_tracing_setup_otlp.py`
- Verify: `tests/test_tracing_cloud_monitor_e2e.py`

- [ ] Run `uv run pytest tests/test_tracing_setup_otlp.py tests/test_tracing_cloud_monitor_e2e.py -q`.
- [ ] Run `uv run ruff check ksadk/tracing/setup.py tests/test_tracing_setup_otlp.py tests/test_tracing_cloud_monitor_e2e.py`.
- [ ] Run `git diff --check` and inspect the final diff for Cloud Monitor-only scope and unchanged Generic/Langfuse exporter construction.
