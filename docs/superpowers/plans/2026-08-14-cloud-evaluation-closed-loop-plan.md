# Cloud Evaluation Closed-Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect immutable cloud Dataset versions to the existing KsADK evaluation executor and traceable reports.

**Architecture:** Keep cloud transport in `ksadk.evaluation` and keep Runtime/Target adapters unchanged. Resolve and validate one remote snapshot at the request boundary, then pass the normalized EvalSet and a `CloudDatasetRef` through the existing executor/report contracts. CLI and Studio call the same cloud service.

**Tech Stack:** Python 3.10+, Pydantic v2 contracts, httpx async client, Click CLI, FastAPI Studio API, pytest/pytest-asyncio.

---

### Task 1: Remote source contract

**Files:**
- Modify: `ksadk/evaluation/contracts.py`
- Test: `tests/test_evaluation_contracts.py`

- [ ] Add a failing test for `CloudDatasetRef` validation and `EvalRunSpec` serialization.
- [ ] Add the model and optional `cloud_dataset` fields to request/spec/report serialization.
- [ ] Run the contract tests and keep the existing digest rules unchanged.

### Task 2: Cloud read service

**Files:**
- Modify: `ksadk/evaluation/cloud_service.py`
- Modify: `ksadk/evaluation/agent_eval_client.py`
- Modify: `ksadk/evaluation/__init__.py`
- Test: `tests/test_evaluation_cloud_service.py`
- Test: `tests/test_evaluation_agent_eval_client.py`

- [ ] Add failing tests for fixed-version reads, digest/schema/row-count validation, and catalog responses.
- [ ] Implement provider protocol methods and the HTTP adapter without implicit current-version reads.
- [ ] Run cloud service/client tests.

### Task 3: CLI remote execution

**Files:**
- Modify: `ksadk/cli/cmd_eval.py`
- Modify: `ksadk/cli/cmd_evalset.py`
- Test: `tests/test_cmd_eval.py`
- Test: `tests/test_cmd_evalset.py`

- [ ] Add failing tests for `evalset pull` and `eval --dataset-id --dataset-version`.
- [ ] Resolve the remote snapshot once, construct `EvaluationRequest`, and reuse `execute_evaluation`.
- [ ] Run CLI tests and verify help output.

### Task 4: Studio cloud source surface

**Files:**
- Modify: `ksadk/studio/service.py`
- Modify: `ksadk/studio/api.py`
- Modify: `ksadk/studio/react-ui/src/pages/EvaluationsPage.tsx`
- Test: `tests/studio/test_evaluation_shell.py`
- Test: `ksadk/studio/react-ui/src/pages/EvaluationsPage.test.tsx`

- [ ] Add failing API tests for cloud catalog and fixed-version preview/pull.
- [ ] Reuse `CloudEvalSetService` and preserve the existing evaluation operation contract.
- [ ] Add the minimal source selector and fixed-version fields to the React page.
- [ ] Run Python and frontend tests.

### Task 5: Verification and integration report

**Files:**
- No product files unless tests expose a defect.

- [ ] Run all evaluation and Studio tests with a repository-owned pytest temp directory.
- [ ] Run Python compilation and frontend type/test checks.
- [ ] Record the live agent-eval/EvalSmith E2E prerequisites and Cloud-3 server-side gaps.
