# OTel-First Observability and 0.6.2 Release Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make custom observability backend-agnostic through OpenTelemetry, prepare the current master/public version alignment analysis, and provide a standalone customer demo file that is not committed.

**Architecture:** Customer agent code emits OpenTelemetry spans, span events, and attributes only. KsADK tracing routes those spans through configured OTLP environment variables, with Langfuse treated as the current default OTLP backend rather than a hard dependency. Public release work remains separate from the code change and must follow `AGENTS.md` and `docs/public-release-workflow.md`.

**Tech Stack:** Python, OpenTelemetry SDK/exporter, pytest, KsADK tracing setup, git worktrees, KsADK public release Makefile targets.

**Execution Status:** Implemented and verified locally. The public `0.6.2` candidate was created in `.worktrees/public-main` at `5b16884`; release/publish steps remain gated by explicit user approval. Internal remote branch push is still not confirmed because the ezone remote rejected or failed branch locking during push attempts.

---

### Task 1: Add Generic OTLP Env Routing

**Files:**
- Modify: `tests/test_tracing_setup_otlp.py`
- Modify: `ksadk/tracing/setup.py`
- Modify: `ksadk/tracing/__init__.py`
- Modify: `docs/ksadk环境变量参考.md`

- [x] **Step 1: Write the failing test**

Add a test showing that `setup_tracing()` uses generic `OTEL_EXPORTER_OTLP_*` env vars when they are present, even if Langfuse env vars are also present.

```python
def test_generic_otlp_env_takes_precedence_over_langfuse_auto_env(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.pre.example.com")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.example.com/otel/v1/traces")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "Authorization=Bearer%20demo,x-custom=value%2Fwith%2Fslashes",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.endpoint == "https://collector.example.com/otel/v1/traces"
    assert exporter.headers == {
        "Authorization": "Bearer demo",
        "x-custom": "value/with/slashes",
    }
    assert len(_FakeHttpOTLPSpanExporter.instances) == 1
    assert len(trace_api.provider.processors) == 1
```

- [x] **Step 2: Run the failing test**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-ksadk uv run pytest tests/test_tracing_setup_otlp.py::test_generic_otlp_env_takes_precedence_over_langfuse_auto_env -q
```

Expected: FAIL because generic OTLP env routing is not implemented.

- [x] **Step 3: Implement minimal generic OTLP support**

In `ksadk/tracing/setup.py`, add helpers:

```python
def _parse_otlp_headers(raw: str) -> dict[str, str]:
    ...

def _build_generic_otlp_http_config() -> Optional[dict]:
    ...
```

Then make `setup_tracing()` add an HTTP OTLP exporter from generic env vars before Langfuse auto-detection, and skip synthesized Langfuse OTLP when generic OTLP env is present and `enable_langfuse` was not explicitly set.

- [x] **Step 3a: Support trace-specific OTLP env**

Also cover standard trace-specific variables so users can keep global OTLP settings for other signals:

- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`
- `OTEL_EXPORTER_OTLP_TRACES_HEADERS`

Expected precedence: trace-specific protocol and headers override global protocol and headers.

- [x] **Step 4: Run targeted tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-ksadk uv run pytest tests/test_tracing_setup_otlp.py -q
```

Expected: all tests in the file pass.

Current result: `5 passed`.

### Task 2: Version Alignment and 0.6.2 Release Readiness

**Files:**
- Read: `AGENTS.md`
- Read: `docs/public-release-workflow.md`
- Read: `pyproject.toml`
- Read: `ksadk/version.py`
- Read: `README.md`
- Read: `.worktrees/public-main/pyproject.toml`
- Read: `.worktrees/public-main/ksadk/version.py`
- Read: `.worktrees/public-main/README.md`

- [x] **Step 1: Verify current master version**

Run:

```bash
rg -n 'version = "|VERSION = "|Current version|当前版本' pyproject.toml ksadk/version.py README.md
```

Expected: master currently reports `0.6.0`.

- [x] **Step 2: Verify public branch version**

Run:

```bash
rg -n 'version = "|VERSION = "|Current version|当前版本' .worktrees/public-main/pyproject.toml .worktrees/public-main/ksadk/version.py .worktrees/public-main/README.md
```

Expected: public branch files currently report `0.6.1`.

- [x] **Step 3: Do not change release versions without approval**

Per `AGENTS.md`, do not edit `pyproject.toml`, `ksadk/version.py`, README version lines, or CHANGELOG release entries until the user explicitly approves the version bump/release action.

- [x] **Step 4: Report 0.6.2 readiness**

Summarize:

```text
Code optimization commit can be prepared on master.
0.6.1 alignment requires explicit version bump approval if master should be changed now.
0.6.2 public candidate requires public workflow: public-preflight, internal review, GitHub main push, tag, PyPI/GitHub Release only after approval.
```

Current status: `.worktrees/public-main` was recreated from `github/main`, and `release/public-0.6.2` now exists locally at `5b16884 feat: prepare ksadk 0.6.2 otel tracing candidate`. Internal ezone branch push still needs a successful retry after the remote lock issue clears.

### Task 3: Standalone Customer Demo

**Files:**
- Create outside git: `/private/tmp/ksadk_custom_otel_observability_demo.py`

- [x] **Step 1: Write demo file outside the repository**

Create a Python file using only `ksadk.tracing` and OpenTelemetry:

```python
from ksadk.tracing import setup_tracing, get_tracer

setup_tracing(enable_inmemory=False)
tracer = get_tracer("customer.deep_research")
```

The demo must include:

- root run span
- tool span
- analysis span
- report span
- `checkpoint.saved` span event
- checkpoint child span to show the difference between a child span and a span event
- score / evaluation child span with `score.*` attributes
- Chinese comments explaining when to use child spans versus span events in Langfuse
- `ksadk.agent_id`, `ksadk.session_id`, `ksadk.user_id`, `ksadk.invocation_id`
- generic `OTEL_EXPORTER_OTLP_*`, trace-specific `OTEL_EXPORTER_OTLP_TRACES_*`, and current `LANGFUSE_*` environment examples in comments

- [x] **Step 2: Verify the demo is outside the repository**

Run:

```bash
git status --short -- /private/tmp/ksadk_custom_otel_observability_demo.py
```

Expected: git reports the path is outside the repository. This confirms the demo cannot be tracked by this repo.

### Task 4: Verification and Status Summary

**Files:**
- Check: `git status --short`
- Check: `git diff -- tests/test_tracing_setup_otlp.py ksadk/tracing/setup.py ksadk/tracing/__init__.py docs/ksadk环境变量参考.md docs/superpowers/plans/2026-06-04-otel-first-observability-release.md docs/superpowers/plans/2026-06-04-ksadk-0.6.2-public-candidate-audit.md`

- [x] **Step 1: Run targeted tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-ksadk uv run pytest tests/test_tracing_setup_otlp.py -q
```

Expected: all tracing OTLP tests pass.

- [x] **Step 2: Inspect final git status**

Run:

```bash
git status --short
```

Expected: many pre-existing unrelated changes remain; new intended changes are limited to OTel tracing, OTel docs, and release-preparation plan/audit files.

- [x] **Step 3: Summarize release gate**

Report that the code optimization is complete only if tests pass. Report that version bump/release remains gated by explicit approval and public release workflow.
